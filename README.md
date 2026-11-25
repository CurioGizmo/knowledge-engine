# MCP Knowledge Engine

<p align="center">
  <b>🇬🇧 English</b> •
  <a href="README.ru.md">🇷🇺 Русский</a>
</p>

> **Long-term memory, step-by-step implementation plans, and customizable engineering workflows for AI agents.**  
> A unified platform of three Model Context Protocol (MCP) servers: **knowledge**, **audit**, and **skills**. All data is stored directly in your Git repository as human-readable Markdown documents.

[![Status: Public Beta](https://img.shields.io/badge/status-public--beta-orange.svg)](https://github.com/CurioGizmo/knowledge-engine)
[![Version: v0.6.0](https://img.shields.io/badge/version-v0.6.0-blue.svg)](https://github.com/CurioGizmo/knowledge-engine/releases)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![uv](https://img.shields.io/badge/uv-package%20manager-8A2BE2.svg)](https://docs.astral.sh/uv/)
[![CI](https://github.com/CurioGizmo/knowledge-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/CurioGizmo/knowledge-engine/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> [!NOTE]
> **Public Beta (v0.6.0) / Early Preview**  
> `knowledge-engine` is currently in active public beta testing. While the engine architecture is verified by a 1,600+ automated test suite, real-world agent workflows, developer experience, and integrations are being actively refined. Feedback, suggestions, and issue reports are very welcome!

---

## 🎯 What Problems Does It Solve?

Most AI coding assistants and autonomous agents (in Cursor, Claude Desktop, Windsurf, Roo Code, Antigravity) excel at short tasks, but encounter fundamental limitations on complex projects:

1. **Session Amnesia:** Agents spend time debugging subtle issues or making architectural trade-offs, only to forget everything in the next session and repeat the same mistakes.
2. **Chaos in Complex Tasks:** When tackling large features, agents often attempt everything at once, lose intermediate context, skip crucial verification steps, and get stuck midway.
3. **Lack of Team Process:** Agents act without guidelines, bypassing team-mandated stages like planning, peer reviews, and automated verification.

**MCP Knowledge Engine provides three integrated engines that transform agents from "forgetful chat bots" into disciplined software engineers:**

---

## 🧠 1. Knowledge Engine — Long-Term Memory & Architectural Decisions

Maintains project engineering context and lessons learned so that discoveries are never lost between sessions:

* **Lessons & Architectural Decision Records (ADRs):** The agent records *why* specific choices were made, which libraries or design patterns are banned, and what gotchas were uncovered.
* **Hypotheses & Antipatterns:** When troubleshooting subtle bugs, the agent logs working hypotheses. If an idea proves to be a dead end or dangerous, it is refuted into an **antipattern** (`⛔`). Before starting any new task, the agent checks the knowledge base and never repeats disproved approaches.
* **Provenance Protection:** Knowledge cards confirmed by human developers are protected against accidental automated overwrite by agents.

---

## 🛠️ 2. Audit Engine — Step-by-Step Plans & Technical Debt Tracking

Enforces engineering rigor and disciplined execution for complex initiatives:

* **Step-by-Step Implementation Plans:** Instead of jumping into uncoordinated code edits, the agent first formulates a structured implementation plan with clear completion criteria.
* **Sequential Step Execution & Proof of Work:** The agent progresses strictly step by step. Each completed step is logged with verifiable proof (test run logs, list of modified files) before moving to the next. Progress is transparent to humans and persists across session restarts.
* **Plan Versioning & Safe Rollback (New in v0.6.0):** Plans are version-controlled with immutable revision history. Substantive definition changes automatically track revisions, and agents or humans can inspect diffs or safely roll back plans to previous versions with conflict detection.
* **Technical Debt Dependency Graph (DAG):** Technical debt and tasks are recorded directly in the repository with dependency relationships (`blocked_by`). The agent automatically pulls only tasks whose prerequisites are completely resolved.
* **Review History:** Code review remarks and feedback are attached to plans and tasks, creating a clear development audit trail.

---

## ⚡ 3. Skills Engine — Modular Skills & Custom Workflows (Flows)

Establishes engineering standards and allows you to enforce custom processes across your team and projects:

* **Shared Skills Across Multiple Projects (Central Catalog):** You don't need to copy skills and instructions into every project manually. Skills Engine can pull from an **external shared Git repository** (`MCP_SKILLS_PATH` env variable or `--skills-dir` argument). Update a skill or workflow once, and every project instantly gets the latest capabilities.
* **Custom Engineering Workflows (Flows):** You can define your team's development processes as simple Markdown files (e.g. *Analysis → Plan → Plan Review → Implementation → Testing → Delivery*). The agent connects to the state machine and follows the prescribed sequence.
* **Step-by-Step Guardrails & Validation:** Each phase enforces strict transition requirements — for example, preventing the agent from writing code until the plan is approved, or prohibiting task completion until tests pass. This ensures the agent never cuts corners or skips critical steps.
* **Project Stack Adaptation:** Skills avoid cluttering the prompt context with irrelevant instructions — the engine detects your active project stack (FastAPI, React, SQLite, Postgres, etc.) and dynamically injects only relevant guidance.
* **Project-Level Local Overrides:** When an individual repository needs unique playbooks or adjustments, you can store them in local `.agents/skills` to complement or override catalog skills.

---

## ⚡ How It Works Under the Hood

* **Project Memory & Plans in Repo (`.agents/`):** Knowledge cards (Knowledge Engine) and implementation plans (Audit Engine) live directly inside the `.agents/` directory of your repository as clean Markdown files with YAML frontmatter. You can version-control, view, and review them in regular Pull Requests.
* **Shared Skills in External Catalog:** The skills and workflows catalog (Skills Engine) is sourced from a central external repository (cached by default at `~/.cache/mcp-skills/repo`), enabling organization-wide engineering consistency across multiple projects without file duplication.
* **Instant Local Search:** The server automatically maintains an in-memory SQLite FTS5 full-text index (`.cache/`). Keyword and semantic searches complete in under 1 millisecond.
* **Zero Infrastructure:** No external databases, Docker containers, subscriptions, or third-party cloud API keys required. All you need is Python 3.11+.

---

## 🚀 Quickstart: Connect to Any AI Client

The platform runs over the standard **Model Context Protocol (stdio)** and connects with **Cursor, Claude Desktop, Windsurf, VS Code (Roo Code / Cline), Antigravity, OpenCode**.

### 1. Client Configuration Options

You **do not need to hardcode project paths** — the servers automatically detect the repository open in your editor (via `.git`, `pyproject.toml`, or the client's working directory) and attach to it dynamically.

#### Option A: Zero-install via `uvx` (Recommended)
Your client runs the servers in isolated environments without pre-installation:

```json
{
  "mcpServers": {
    "knowledge-engine": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/CurioGizmo/knowledge-engine.git", "mcp-knowledge-engine"]
    },
    "audit-engine": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/CurioGizmo/knowledge-engine.git", "mcp-audit-engine"]
    },
    "skills-engine": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/CurioGizmo/knowledge-engine.git", "mcp-skills-engine"]
    }
  }
}
```

#### Option B: Global installation via `uv tool`
If you installed the platform globally:
```bash
uv tool install git+https://github.com/CurioGizmo/knowledge-engine.git
```
Simply specify the command names without arguments or paths:
```json
{
  "mcpServers": {
    "knowledge-engine": {
      "command": "mcp-knowledge-engine"
    },
    "audit-engine": {
      "command": "mcp-audit-engine"
    },
    "skills-engine": {
      "command": "mcp-skills-engine"
    }
  }
}
```

*(For local engine development from source, you can pass `args: ["run", "--directory", "/path/to/mcp-knowledge-engine", "..."]`).*

> 💡 **For Antigravity / Gemini CLI Users:**  
> All three engines and companion skills can be configured with a single command:  
> `uv run mcp-engine-install --skill`

---

## 📦 Releases & Packaging

Pre-built wheel (`.whl`) and source archive (`.tar.gz`) packages are automatically generated and attached to each GitHub Release via GitHub Actions.

To build distribution packages locally:
```bash
uv build
```
Built artifacts appear in `dist/` and can be installed into any Python environment:
```bash
uv tool install dist/mcp_knowledge_engine-0.5.0-py3-none-any.whl
```

---

## ❓ Frequently Asked Questions (FAQ)

**Where are files physically saved?**  
All data is saved in the `.agents/` folder at the root of your project:
* `.agents/knowledge/` — Lessons, notes, ADRs, and antipatterns.
* `.agents/plans/` — Step-by-step implementation plans.
* `.agents/audit/` — Technical debt tasks (`todo/` and `done/`).
* `.agents/flows/` — Your custom engineering workflows.

**How does the server know which project to target?**  
The server automatically discovers the project root by walking up from the current working directory to `.git`, `pyproject.toml`, or `package.json`. You can also supply it explicitly via `--project-root /path/to/project` or the `MCP_PROJECT_ROOT` environment variable.

**What if the search cache gets out of date?**  
The cache is strictly ephemeral. Simply delete `.agents/.cache/*_fts.db` — the server will re-index all Markdown cards on the next call in less than 50ms.

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.
