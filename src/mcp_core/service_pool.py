"""Потокобезопасный LRU-пул композитных сервисов проектов для multi-tenant режима."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

logger = logging.getLogger(__name__)

TBundle = TypeVar("TBundle")

# Active tool-call scope: roots already leased in the CURRENT (innermost) scope.
# A ContextVar per async task/sync worker thread, so concurrent tool calls never
# share scope state. reset(token) in release_scope also drops any child scopes.
_scope_stack: ContextVar[_Scope | None] = ContextVar("mcp_service_pool_scope", default=None)


class _Scope(dict[Path, None]):
    """Roots leased by the current tool call + token для отката contextvar."""

    __slots__ = ("token",)

    def __init__(self) -> None:
        super().__init__()
        self.token: object = None


class ProjectServicePool(Generic[TBundle]):
    """Потокобезопасный LRU-пул композитных сервисов проектов с поддержкой аренды (leases).

    Обеспечивает изоляцию ресурсов разных проектов, динамическую инициализацию
    по ключу канонического корня, потокобезопасное создание (Double-Checked Locking)
    и ограничение количества активных инстансов с вызовом bundle.close() при вытеснении.
    Арендованные бандлы (активные запросы) защищены от вытеснения и закрытия.
    """

    def __init__(
        self,
        factory: Callable[[Path], TBundle],
        max_size: int = 16,
    ) -> None:
        self._factory = factory
        self._max_size = max(1, max_size)
        self._pool: OrderedDict[Path, TBundle] = OrderedDict()
        self._pool_lock = threading.Lock()
        self._key_locks: dict[Path, list[Any]] = {}  # [threading.Lock, ref_count: int]
        self._meta_lock = threading.Lock()
        self._leases: dict[Path, int] = {}

    def _evict_unleased_under_lock(self) -> list[TBundle]:
        """Вытесняет старейшие неарендованные бандлы, пока размер пула превышает max_size.

        Должен вызываться ТОЛЬКО под self._pool_lock.
        """
        evicted: list[TBundle] = []
        while len(self._pool) > self._max_size:
            # Ищем самый старый проект в LRU-порядке, на который нет активных аренд
            unleased_key = next(
                (k for k in self._pool if self._leases.get(k, 0) <= 0),
                None,
            )
            if unleased_key is None:
                # Все бандлы в пуле заняты активными запросами — допускаем временный burst
                break
            evicted.append(self._pool.pop(unleased_key))
        return evicted

    def _get_bundle(self, canonical_root: Path, hold_lease: bool = False) -> TBundle:
        # 1. Быстрый путь: проверка в пуле под общим lock
        with self._pool_lock:
            if canonical_root in self._pool:
                self._pool.move_to_end(canonical_root)
                if hold_lease:
                    self._leases[canonical_root] = self._leases.get(canonical_root, 0) + 1
                return self._pool[canonical_root]

        # 2. Получение лока для конкретного ключа с подсчётом ссылок (ref-count)
        with self._meta_lock:
            if canonical_root not in self._key_locks:
                self._key_locks[canonical_root] = [threading.Lock(), 0]
            entry = self._key_locks[canonical_root]
            entry[1] += 1
            key_lock: threading.Lock = entry[0]

        evicted: list[TBundle] = []
        try:
            # 3. Синхронизированная инициализация конкретного проекта
            with key_lock:
                with self._pool_lock:
                    if canonical_root in self._pool:
                        self._pool.move_to_end(canonical_root)
                        if hold_lease:
                            self._leases[canonical_root] = self._leases.get(canonical_root, 0) + 1
                        return self._pool[canonical_root]

                # Создание бандла вне pool_lock
                bundle = self._factory(canonical_root)

                with self._pool_lock:
                    self._pool[canonical_root] = bundle
                    self._pool.move_to_end(canonical_root)
                    if hold_lease:
                        self._leases[canonical_root] = self._leases.get(canonical_root, 0) + 1
                    evicted = self._evict_unleased_under_lock()

            return bundle
        finally:
            with self._meta_lock:
                entry[1] -= 1
                if entry[1] <= 0:
                    self._key_locks.pop(canonical_root, None)

            # Закрываем вытесненные ресурсы
            for ev in evicted:
                self._close_bundle(ev)

    def get(self, root: Path) -> TBundle:
        """Получает или инициализирует бандл для проекта с ротацией LRU (без удержания аренды)."""
        return self._get_bundle(root.resolve(), hold_lease=False)

    def acquire(self, root: Path) -> TBundle:
        """Атомарно получает бандл и регистрирует активную аренду (lease)."""
        return self._get_bundle(root.resolve(), hold_lease=True)

    def release(self, root: Path) -> None:
        """Освобождает аренду бандла и выполняет отложенное вытеснение при превышении лимита."""
        canonical_root = root.resolve()
        evicted: list[TBundle] = []
        with self._pool_lock:
            if canonical_root in self._leases:
                self._leases[canonical_root] -= 1
                if self._leases[canonical_root] <= 0:
                    self._leases.pop(canonical_root, None)
            evicted = self._evict_unleased_under_lock()

        for ev in evicted:
            self._close_bundle(ev)

    @contextmanager
    def lease(self, root: Path) -> Generator[TBundle, None, None]:
        """Контекстный менеджер безопасной аренды бандла на время выполнения запроса."""
        bundle = self.acquire(root)
        try:
            yield bundle
        finally:
            self.release(root)

    def start_scope(self) -> None:
        """Открывает scope аренды на весь вызов MCP-тула.

        Регистрирует новый словарь корней в contextvar-стеке; конкурентные вызовы
        (asyncio-таски / потоки) получают независимые скоупы. Всегда парный к
        :meth:`release_scope` (вызывается в finally обёртки тула).
        """
        scope = _Scope()
        scope.token = _scope_stack.set(scope)

    def scoped_get(self, root: Path) -> TBundle:
        """Арендует бандл на текущий tool-call scope (идемпотентно по корню).

        Повторный вызов с тем же корнем в том же скоупе не наращивает счётчик
        (resolver и телеметрия в finally делят одну аренду). Вызов без активного
        скоупа — программная ошибка (лиз нечем будет освобождать).
        """
        scope = _scope_stack.get()
        if scope is None:
            raise RuntimeError(
                "scoped_get requires an active tool-call scope (start_scope/release_scope)"
            )
        canonical = root.resolve()
        if canonical not in scope:
            bundle = self.acquire(root)
            scope[canonical] = None
            return bundle
        return self.get(root)

    def release_scope(self) -> None:
        """Освобождает все аренды текущего скоупа (в finally обёртки тула).

        Откатывает contextvar к значению до start_scope: вложенные/потерянные
        дочерние скоупы сбрасываются вместе с текущим. Идемпотентна (повторный
        вызов без активного скоупа — no-op).
        """
        scope = _scope_stack.get()
        if scope is None:
            return
        _scope_stack.reset(cast("Token[_Scope | None]", scope.token))
        for canonical_root in scope:
            self.release(canonical_root)

    @staticmethod
    def _close_bundle(bundle: Any) -> None:
        """Безопасно вызывает bundle.close(), если метод существует."""
        if hasattr(bundle, "close") and callable(bundle.close):
            try:
                bundle.close()
            except Exception as e:
                logger.warning("Ошибка при закрытии сервисного бандла %s: %s", bundle, e)

    def close_all(self) -> None:
        """Закрывает и очищает все бандлы в пуле."""
        with self._pool_lock:
            bundles = list(self._pool.values())
            self._pool.clear()
            self._leases.clear()
        for b in bundles:
            self._close_bundle(b)
