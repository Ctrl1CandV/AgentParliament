<div align="center">

# 🏛️ AgentParliament

**An MCP service for multi-agent collaboration — using role division and cross-validation to push engineering quality beyond what any single model can achieve**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MCP Protocol](https://img.shields.io/badge/MCP-1.2+-6366F1)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[中文](README.md) | **English**

</div>

---

## Why AgentParliament?

No matter which model you use — top-tier Claude Opus or domestic models like DeepSeek, GLM — single-model output suffers from three structural flaws that no amount of raw intelligence can fix:

| Problem | Symptom |
|---|---|
| **Blind spots** | The model can't see what it's missing — it doesn't know what it doesn't know |
| **Confirmation bias** | Models tend to affirm their own conclusions rather than critically examine them; the coder believes the code is correct, the architect believes the design is sound |
| **Paper tigers** | A plan "looks feasible" until you actually run it; tests "look covered" but can't catch real bugs |

**AgentParliament's approach isn't "use a stronger model" — it's "use collaboration to break single-model confirmation bias."** Multiple models play different roles — planner, developer, attacker, reviewer — examining the same artifact from adversarial perspectives, cross-validating and catching each other's errors. One person acting as both player and referee will systematically overestimate their own output; handing judgment to an external model that didn't participate in the production breaks the bias.

**Core insight**: The "approaching top-tier models with domestic models" narrative is becoming obsolete — models are getting smarter, but the engineering quality bottleneck isn't "is the model smart enough," it's "is anyone looking at it from the opposing side." AgentParliament turns that "opposing side" into reusable infrastructure.

---

## What can it do?

### 10 tools across a three-tier capability ladder

AgentParliament splits sub-agent capabilities into three tiers. Each tool explicitly declares which tier it belongs to — rather than all tools sharing the same read-only policy:

| Tier | Capability | Mechanism | Tools |
|---|---|---|---|
| **Tier 0** | Pure text fusion, no file access | Direct API call (no subprocess overhead) | consensus synthesis, advisor escalation, chain synthesize |
| **Tier 1** | Read-only exploration, can read code to verify | CLI `default` + read-only allowlist + write blocklist | delegate_research, independent_analysis, validate_approach, test_audit, peer_review, consensus first round |
| **Tier 2** | Isolated writable & executable, can run real tests | git worktree isolated copy + `--dangerously-skip-permissions` | verify_implementation |

### The 10 tools

| Tool | What it does | Typical scenario |
|---|---|---|
| **delegate_research** | Delegate a read-only sub-agent to research and return structured conclusions | Complex questions needing a second perspective, or offloading research from the main context |
| **peer_review** | Graded review of `git diff` (🔴critical/🟠major/🟡minor/🟢suggestion), supports `structured=True` JSON | Want another model to independently verify after implementation |
| **independent_analysis** | Let a third-party model critically examine an existing conclusion for blind spots | Got a conclusion but still uncertain, want to verify reliability |
| **consensus** | Parallel multi-model responses to the same question, auto-comparing consensus & dissent; optional `synthesize=True` for fused conclusion | Key decisions needing multi-model cross-validation |
| **validate_approach** | Have a model play the adversary, finding holes in an architecture/plan | Stress-test a plan before implementation |
| **test_audit** | Static analysis of source and tests, finding uncovered branches and boundaries | Want to know "what cases aren't tested yet" |
| **advisor_analysis** | Two-stage strategic help: mid-tier model drafts first, escalates to strong model only when uncertain | Use cheap models as baseline, escalate to strong model at critical points |
| **delegate_chain** | Multi-step reasoning chain: main agent defines the chain structure, executed sequentially | Important decisions needing a full pipeline (research→scrutinize→verify) |
| **delegate_dialogue** | Research with dialogue: sub-agent can ask the main agent via `[ASK_PARENT]` | Research with directional分歧 requiring main agent decisions |
| **verify_implementation** | Design and execute tests in an isolated git worktree, producing ground truth + suggested diff | Want to actually run tests rather than static analysis |

---

## Real-world results

We built a "trap project" `minibank-trap` to test: a mini banking system with **6 deliberately planted graded defects** (2 critical/2 major/2 minor) + 5 design-level vulnerabilities + 2 project-specific rule violations only discoverable by reading project memory. The answer key was kept private — the model had to find the landmines itself.

| What we tested | What it tested | Score |
|---|---|---|
| `peer_review` | How many of 6 known code defects it could find | **6 / 6**, each with line numbers and fix suggestions |
| `validate_approach` | How many of 5 design vulnerabilities it could block | **5 / 5** |
| Shared memory injection | Project constraints hidden in `CLAUDE.md` | Only caught violations reliably when `project_dir` was passed |
| Strategic escalation | Can a strong model rescue a mid-tier model's mistake | Claude corrected the draft's errors and found the **most severe issue the draft missed** |

In short: **a team of mid-tier models dug out problems that only top-tier models could reliably find.**

---

## Core features

### 🏗️ Three-tier capability ladder — from "absolutely read-only" to "isolated execution"

Early versions had all tools share an "absolutely read-only" rule. With verify_implementation, "read-only" was split into a capability ladder:

- **Tier 0** (pure text fusion): Direct API call via httpx, no CLI subprocess overhead. For consensus synthesis, advisor escalation, chain synthesis — tasks where materials are already collected
- **Tier 1** (read-only exploration): `default` mode + read-only allowlist (Read/Grep/Glob/read-only git) + write blocklist. Sub-agents can actively read code to verify conclusions, not just critique prompt text
- **Tier 2** (isolated writable & executable): git worktree isolated copy + `--dangerously-skip-permissions`. Sub-agents can create test files, modify source, actually run pytest for ground truth. Changes are returned as **suggested diffs** for main agent approval — the main repo working tree stays byte-level unchanged

### 🔀 CLI / API dual backend — auto-routed by scenario

Tier 0 goes through direct API calls (httpx to `/v1/messages`); Tier 1/2 go through Claude Code CLI (built-in file system tools and sandbox). Both share the same **failure chain + circuit breaker**, with consistent error classification — transparent to callers.

### 🔄 Role failure chain — automatic degradation, never returns empty

Each role has a model priority chain. If the preferred model is unavailable, it automatically degrades to the next; only reports error if all fail:

```json
"roles": {
  "researcher": ["deepseek", "glm", "minimax"],
  "reviewer": ["glm", "mimo", "deepseek"],
  "strong_aggregator": ["claude", "glm"],
  "advisor": ["claude", "glm"]
}
```

### ⚡ Lightweight circuit breaker — don't waste timeouts on dead models

Each model maintains a consecutive failure count; after 3 failures it's skipped for a 5-minute cooldown. **Only "model unavailable" errors (auth/rate-limit/network/model-id) count** — timeouts and unknown errors don't, since a timeout might mean the model is thinking hard. CLI/API share the same circuit breaker state.

### 🧠 Shared memory injection — making sub-agents "understand your project"

Before each call, explicitly reads the project root `CLAUDE.md` (domain language, architecture decisions, hard constraints) and injects it at the prompt start. Also injects SPEC.md progress/next-step + ADR index, forming a "read-back loop." Not passing `project_dir` triggers a warning.

### 🎯 Strategic escalation — mid-tier models do the heavy lifting, strong models at critical points

`advisor_analysis` is two-stage: a mid-tier model drafts first, escalating to a strong model only when it self-assesses uncertainty (`[NEED_ADVISOR]`) or `force_advisor=True`. Most calls cost only one mid-tier model invocation.

### 🧩 Multi-model fusion — consensus delivers real "model mixing"

`consensus(synthesize=True)` collects parallel model responses, then uses an aggregator model to critically adjudicate disagreements and fuse a single high-quality conclusion, while preserving all raw responses for traceability.

### ⛓️ Multi-step reasoning chains & dialogue research

- **delegate_chain**: Strings multiple tools into a thinking chain (research→scrutinize→verify); sub-agents within the chain can read all project files
- **delegate_dialogue**: Research with dialogue capability; sub-agents output `[ASK_PARENT]` when hitting directional disagreements, main agent continues with an answer

### 🔒 Security boundaries

- **Tier 0/1**: Sub-agents never modify main repo files. Tier 1 uses allowlist + blocklist dual guardrails — even injected malicious prompts can't break through
- **Tier 2**: worktree is created from an **uncommitted-changes dangling snapshot** (independent temp index, main repo byte-level unchanged); sub-agent changes return as suggested diffs for main agent approval. It isolates the file tree, not the OS process — this is a conscious trade-off of "running not-fully-trusted code"
- **Environment isolation**: Each model gets an independent `CLAUDE_CONFIG_DIR`; all interfering prefix variables are cleaned, eliminating ccswitch interference

### 📊 Structured output + token counting

`peer_review(structured=True)` returns a JSON array (severity/file/line/description/suggestion), directly `json.loads()`-able. Cost is shown as token counts (endpoint-agnostic), no longer in USD (different users use different endpoints, making USD meaningless).

---

## Quick start

### Prerequisites

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated

### 1. Clone the project

```bash
git clone https://github.com/your-username/AgentParliament.git
cd AgentParliament
```

### 2. Configure models

Copy the template and fill in your API endpoints and keys:

```bash
cp profiles.example.json profiles.json
```

`profiles.json` format:

```json
{
  "defaults": {
    "timeout_seconds": 300,
    "role_overrides": {
      "reviewer": 120,
      "third_party": 180,
      "researcher": 300
    }
  },
  "models": {
    "deepseek": {
      "base_url": "https://api.deepseek.com/anthropic",
      "token": "sk-your-token",
      "model": "deepseek-v4-pro"
    },
    "glm": {
      "base_url": "https://your-endpoint/api/coding",
      "token": "your-token",
      "model": "glm-5.2"
    }
  },
  "roles": {
    "researcher": ["deepseek", "glm"],
    "third_party": ["glm", "deepseek"],
    "reviewer": ["glm", "deepseek"],
    "aggregator": ["glm", "deepseek"],
    "strong_aggregator": ["glm", "deepseek"],
    "advisor": ["glm", "deepseek"]
  }
}
```

> Any Anthropic-compatible API endpoint works (DeepSeek, GLM, Mimo, MiniMax, self-hosted vLLM, etc.).
> If you have a top-tier model (e.g. Claude Opus), add a `claude` entry to `models`, put it at the front of `strong_aggregator`/`advisor` chains, and you get strong-model help at critical decisions.

### 3. Configure your MCP client

Add this to your MCP client config (ZCode / Trae / Claude Desktop, etc.):

```json
{
  "mcpServers": {
    "AgentParliament": {
      "command": "uv",
      "args": [
        "run", "--directory",
        "/path/to/AgentParliament",
        "agent-parliament"
      ],
      "timeoutMs": 600000
    }
  }
}
```

> ZCode requires `"timeoutMs": 600000`, otherwise the default 30s will time out.

### 4. Start using

Call directly in conversation:

```
Use peer_review to review my recent code changes, focusing on concurrency safety
```

```
Use verify_implementation to verify if this caching approach actually works — run tests in an isolated copy
```

```
Use test_audit to analyze the test coverage of runner.py
```

---

## Using with the agent-parliament plugin (recommended)

AgentParliament is an MCP tool; for maximum value, pair it with the standalone **agent-parliament plugin** — it contains an orchestrator dispatch guide + six role skills that intervene at different development stages, guiding which tool to call when:

| Skill | Stance | When to intervene | Primary tools |
|---|---|---|---|
| `orchestrator` | Routing | When unsure which role to use | (dispatch layer, doesn't call tools directly) |
| `project-planner` | Think clearly | Pre-build requirements/research/architecture; create/revise implementation plans | `delegate_research`, `validate_approach`, `consensus` |
| `adversary` | Attack the plan (external) | Adversarially refine SPEC/PLAN before the plan is finalized | `validate_approach`, `peer_review`, `independent_analysis` |
| `code-developer` | Build | Turning plans into code, fixing bugs | `peer_review`, `test_audit`, `delegate_research` |
| `reviewer` | Review the implementation (external) | Comprehensive review after implementation, decide minor fix or new plan | `verify_implementation`, `peer_review`, `test_audit` |
| `untangler` | See clearly | Stuck, direction unclear | `independent_analysis`, `validate_approach` |
| `memory-keeper` | Guard | Stage archival, doc-code inconsistency | `delegate_research`, `independent_analysis` |

> **adversary and reviewer are two red-team layers at different times**: adversary attacks the **plan layer** (SPEC/PLAN, no code yet) **before the plan is finalized** — minor issues flow back to planner, major issues in the draft phase can directly modify the original plan; reviewer reviews the **implementation layer** (code + actual changes) **after implementation** — minor issues go to developer for fixing, major issues produce a successor PLAN as a second-round proposal. **Code fixes always flow back to code-developer** — neither adversary nor reviewer writes code.

The plugin's skills define "which tool to call when" (dispatch guide); role personas are defined by each client's subagent config. They work together. Shared iron rule: **MCP tools are for cross-validation, not replacing thinking — form your own conclusion first, then use tools to confirm or falsify.**

> This role division is optional. You can also directly call the 10 tools in any MCP-compatible client.

> **Naming clarification**: The `role` parameter of MCP tools (e.g. `peer_review(role="reviewer")`) is a **model failure-chain name** in `profiles.json` (a list of models tried in priority order), and is **a different namespace** from the plugin's skill role names (e.g. the `reviewer` skill). The name collision is coincidental — MCP's `reviewer` refers to a model chain, plugin's `reviewer` refers to the post-implementation review persona.

---

## Project structure

```
AgentParliament/
├── server.py              # MCP interface layer, 10 tool handlers
├── prompts.py             # Prompt pure functions + memory block injection
├── chain.py               # delegate_chain orchestration + delegate_dialogue session + tool dispatcher
├── render.py              # Result rendering + structure validation
├── runner.py              # Execution: BaseRunner/CLIRunner/APIRunner, failure chains, circuit breaker
├── worktree_manager.py    # verify_implementation's git worktree isolation lifecycle management
├── profiles.json          # Model & role config (gitignored, contains sensitive tokens)
├── profiles.example.json  # Config template
├── mcp.config.json        # MCP client config template
├── pyproject.toml         # Project metadata & dependencies
└── docs/                  # ADRs + design docs (gitignored)
```

---

## FAQ

<details>
<summary><b>Why both CLI and API backends?</b></summary>

CLI (Claude Code CLI) provides built-in file system access (Read/Grep/Glob) and a read-only sandbox, suitable for Tier 1/2 scenarios that need to read code or run tests; direct API calls (httpx) avoid subprocess overhead, suitable for Tier 0 pure text fusion tasks (like consensus synthesis). Both share the same failure chain and circuit breaker, auto-routed by scenario, transparent to callers.

</details>

<details>
<summary><b>Which models are supported?</b></summary>

Any Anthropic-compatible API endpoint. Verified: DeepSeek, GLM, Mimo, MiniMax, Claude Opus (as strong model), LongCat, etc. Self-hosted vLLM/TGI endpoints work as long as they implement `/v1/messages`. The API path sends both `x-api-key` + `Authorization: Bearer`, compatible with both gateway types.

</details>

<details>
<summary><b>Will sub-agents modify my files?</b></summary>

- **Tier 0/1**: No. Read-only allowlist + write blocklist dual guardrails — even injected malicious prompts can't break through
- **Tier 2** (verify_implementation): Writable and executable inside an isolated git worktree copy, but the main repo working tree stays byte-level unchanged (created from an uncommitted-changes dangling snapshot with an independent temp index). Sub-agent changes return as suggested diffs for main agent approval — you decide whether to merge

</details>

<details>
<summary><b>What's the security boundary of verify_implementation?</b></summary>

The worktree isolates the **file tree**, not the **OS process**. Code executed by the sub-agent inside the copy can theoretically still access the file system outside the copy and the network — this is a conscious trade-off of "running not-fully-trusted code." Security relies on three layers: (1) worktree snapshot isolation ensures zero main repo changes; (2) tool blocklist (rm/git push/curl etc. forbidden); (3) suggested diffs for main agent approval. Use cautiously for high-risk projects.

</details>

<details>
<summary><b>How to troubleshoot prompt truncation or model call failures?</b></summary>

**Pipeline self-check**:

```bash
uv run --directory /path/to/AgentParliament python runner.py --selfcheck
```

Runs a multi-line probe prompt to confirm the prompt is fully delivered. Failure clearly indicates whether it's a model call failure or the last line wasn't returned (suspected truncation).

**Debug logging**: Set `AGENTPARLIAMENT_DEBUG=1`, then each subprocess call's real results (returncode, key env vars, stdout/stderr) are written to `logs/debug.log`, with credential fields redacted.

</details>

---

## License

MIT
