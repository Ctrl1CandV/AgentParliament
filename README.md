<div align="center">

# 🏛️ AgentParliament

**多智能体协作的 MCP 服务——用角色分工与交叉验证，把工程质量抬到单模型达不到的高度**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MCP Protocol](https://img.shields.io/badge/MCP-1.2+-6366F1)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**中文** | [English](README.en.md)

</div>

---

## 为什么需要 AgentParliament？

无论用什么模型——顶级的 Claude Opus 还是国产的 DeepSeek、GLM——单模型输出都有三个绕不开的结构性缺陷：

| 问题 | 表现 |
|---|---|
| **盲区** | 模型看不到自己遗漏了什么——它不知道自己不知道什么 |
| **确认偏差** | 模型倾向于肯定自己的结论，而非批判性审视；写代码的相信代码对，做方案的相信方案通 |
| **纸上谈兵** | 方案"看起来可行"，实际跑起来才发现不对；测试"看起来覆盖了"，实际抓不住 bug |

**AgentParliament 的解法不是"换更强的模型"，而是"用协作打破单模型的确认偏差"**：让多个模型扮演不同立场——规划者、开发者、攻击者、复盘者——从对抗性视角审视同一份产出，交叉验证、互相纠错。一个人既当运动员又当裁判会系统性高估自己；把判断交给一个没参与产出的外部模型，确认偏差就被打破。

**核心洞察**：单模型时代的"逼近顶级模型"叙事正在失效——模型越来越强，但工程质量的瓶颈不在"模型够不够聪明"，而在"有没有人从对立面挑毛病"。AgentParliament 把这个"对立面"做成了可复用的基础设施。

---

## 它能做什么？

### 10 个工具，三级能力阶梯

AgentParliament 把副 Agent 的能力拆成三级，每个工具显式声明自己该在哪一级——而不是全体共享同一档只读策略：

| 级别 | 能力 | 承载方式 | 工具 |
|---|---|---|---|
| **Tier 0** | 纯文本融合，不碰文件 | API 直连（省去子进程开销） | consensus 合成、advisor 顾问、chain synthesize |
| **Tier 1** | 只读探索，能主动读代码核实 | CLI `default` + 只读白名单 + 写黑名单 | delegate_research、independent_analysis、validate_approach、test_audit、peer_review、consensus 首轮 |
| **Tier 2** | 隔离可写可执行，能真跑测试 | git worktree 隔离副本 + `--dangerously-skip-permissions` | verify_implementation |

### 10 个工具一览

| 工具 | 做什么 | 典型场景 |
|---|---|---|
| **delegate_research** | 委托副模型只读调研，回传结构化结论 | 复杂问题需要第二视角，或想把调研从主会话剥离 |
| **peer_review** | 对 `git diff` 分级审查（🔴严重/🟠重要/🟡次要/🟢建议），支持 `structured=True` JSON 输出 | 完成实现后让另一个模型独立核查 |
| **independent_analysis** | 让第三方模型批判性审视已有结论，找盲区与逻辑漏洞 | 拿到结论后仍不确定、想验证可靠性 |
| **consensus** | 并行多模型回答同一问题，自动对比共识与分歧；可选 `synthesize=True` 融合单一结论 | 关键决策需要多模型交叉验证 |
| **validate_approach** | 让模型扮演反对者，对架构/方案找漏洞 | 方案实施前提前排雷 |
| **test_audit** | 静态分析源码与测试，找出未覆盖的逻辑分支与边界 | 想知道"哪些情况还没测到" |
| **advisor_analysis** | 两阶段战略求助：中等模型先出草稿，不确定时才升级求助强模型 | 想用便宜模型兜底、关键处再求助强模型 |
| **delegate_chain** | 多步推理链：主 Agent 定义链路结构，依次执行，链内子 Agent 可全文件读取 | 重要决策需要完整链路（调研→审视→验证） |
| **delegate_dialogue** | 带对话能力的调研：子 Agent 可通过 `[ASK_PARENT]` 向主 Agent 提问 | 调研中存在方向性分歧、需主 Agent 拍板 |
| **verify_implementation** | 在 git worktree 隔离副本中设计并执行测试，产出 ground truth + 建议性 diff | 想真正运行测试验证可行性，而非静态分析 |

---

## 真实效果

我们造了一个「陷阱项目」`minibank-trap` 来实测：一个迷你银行账户系统，**故意埋了 6 个分级缺陷**（2 致命/2 重要/2 次要）+ 5 个方案级设计漏洞 + 2 条只有读了项目记忆才能发现的「项目专属规则」违背。标准答案私有保存、绝不喂给模型——模型得自己把雷挖出来。

| 拿什么考它 | 考了什么 | 成绩 |
|---|---|---|
| `peer_review` | 6 个已知代码缺陷能挖出几个 | **6 / 6 全中**，每个带行号和修复建议 |
| `validate_approach` | 5 个方案设计漏洞能拦下几个 | **5 / 5 全拦** |
| 共享记忆注入 | 藏在 `CLAUDE.md` 里的项目硬约束 | 传了 `project_dir` 才稳定揪出违规 |
| 战略求助 | 中等模型出错时强模型能不能救场 | Claude 纠正了草稿错误，还补出了草稿**漏掉的最严重问题** |

一句话：**几个中等模型组队，把顶级模型才能稳定发现的问题挖了出来。**

---

## 核心特性

### 🏗️ 三级能力阶梯——从"绝对只读"到"隔离可执行"

早期版本所有工具共享"绝对只读"铁律。但随着 verify_implementation 的加入，"只读"被拆成能力阶梯：

- **Tier 0**（纯文本融合）：走 API 直连，省去 CLI 子进程启动开销。适合 consensus 合成、advisor 顾问、chain 综合等"材料已收集好"的融合任务
- **Tier 1**（只读探索）：`default` 模式 + 只读工具白名单（Read/Grep/Glob/只读 git）+ 写黑名单。副 Agent 能主动读代码核实结论，而非只对着 prompt 文本挑毛病
- **Tier 2**（隔离可写可执行）：git worktree 隔离副本 + `--dangerously-skip-permissions`。副 Agent 能新建测试文件、修改源码、真正运行 pytest 拿 ground truth。改动以**建议性 diff** 交主 Agent 审批，主仓库工作树字节级不变

### 🔀 CLI / API 双后端——按场景自动路由

Tier 0 走 API 直连（httpx 调 `/v1/messages`），Tier 1/2 走 Claude Code CLI（自带文件系统工具与沙箱）。两者共享同一套**角色失败链 + 熔断器**，错误分类产出一致，对调用方透明。

### 🔄 角色失败链——自动降级，永不空回

每个角色配置一条模型优先级链，首选不可用时自动降级，全失败才报错：

```json
"roles": {
  "researcher": ["deepseek", "glm", "minimax"],
  "reviewer": ["glm", "mimo", "deepseek"],
  "strong_aggregator": ["claude", "glm"],
  "advisor": ["claude", "glm"]
}
```

### ⚡ 轻量熔断器——不浪费超时在挂掉的模型上

每个模型维护连续失败计数，达 3 次后冷却 5 分钟内直接跳过。**只有"模型不可用"类错误（鉴权/限流/网络/模型ID）才计入**，超时和未知错误不算——超时可能是模型在认真思考。CLI/API 共享同一套熔断状态。

### 🧠 共享记忆注入——让副 Agent「懂这个项目」

每次调用前显式读取项目根目录 `CLAUDE.md`（领域语言、架构决策、硬约束）注入 prompt 开头，使副 Agent 确定性获得项目上下文。还增量注入 SPEC.md 进度/下一步 + ADR 索引，形成"回读闭环"。不传 `project_dir` 会标注警告。

### 🎯 战略求助——中等模型扛大头，关键处求助强模型

`advisor_analysis` 两阶段：中等模型先出草稿，仅自评不确定（`[NEED_ADVISOR]`）或 `force_advisor=True` 时才升级强模型。大多数调用只花一次中等模型成本。

### 🧩 多模型融合——consensus 真正 deliver「拼好模」

`consensus(synthesize=True)` 并行收集各模型回答后，用聚合模型批判性裁决分歧、融合单一高质量结论，保留各模型原始回答供追溯。

### ⛓️ 多步推理链与对话调研

- **delegate_chain**：把多个工具串成思考链（调研→审视→验证），链内子 Agent 可读项目全部文件
- **delegate_dialogue**：带对话能力的调研，子 Agent 遇方向性分歧时输出 `[ASK_PARENT]` 提问，主 Agent 带 `answer` 续答

### 🔒 安全边界

- **Tier 0/1**：副 Agent 永不修改主仓库文件。Tier 1 用白名单 + 黑名单双重护栏，即使 prompt 被注入恶意指令也无法突破
- **Tier 2**：worktree 基于未提交改动的**游离快照**创建（独立临时索引，主仓库字节级不变），子 Agent 改动以建议性 diff 交主 Agent 审批。隔离的是文件树不是进程——这是"运行不完全受控代码"的有意识取舍
- **环境隔离**：每个模型独立 `CLAUDE_CONFIG_DIR`，清理所有干扰前缀变量，杜绝 ccswitch 串扰

### 📊 结构化输出 + token 计数

`peer_review(structured=True)` 返回 JSON 数组（severity/file/line/description/suggestion），可直接 `json.loads()`。成本以 token 计数展示（端点无关），不再展示美元价格（不同用户用不同端点，美元无意义）。

---

## 快速开始

### 前置条件

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)（Python 包管理器）
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 已安装并登录

### 1. 克隆项目

```bash
git clone https://github.com/your-username/AgentParliament.git
cd AgentParliament
```

### 2. 配置模型

复制模板并填入你的 API 端点和密钥：

```bash
cp profiles.example.json profiles.json
```

`profiles.json` 格式：

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

> 任何兼容 Anthropic API 格式的端点均可（DeepSeek、GLM、Mimo、MiniMax、自部署 vLLM 等）。
> 若你有顶级模型（如 Claude Opus），在 `models` 加一个 `claude` 条目，放进 `strong_aggregator`/`advisor` 链首，即可在关键决策时求助强模型。

### 3. 配置 MCP 客户端

将以下内容添加到你的 MCP 客户端配置（ZCode / Trae / Claude 桌面版等）：

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

> ZCode 必须加 `"timeoutMs": 600000`，否则默认 30 秒会超时。

### 4. 开始使用

在对话中直接调用即可：

```
请用 peer_review 审查我最近的代码改动，重点关注并发安全性
```

```
用 verify_implementation 验证这个缓存方案是否真的可行，在隔离副本里跑一遍测试
```

```
用 test_audit 分析 runner.py 的测试覆盖情况
```

---

## 配合 agent-parliament plugin 使用（推荐）

AgentParliament 是 MCP 工具；要发挥最大价值，推荐配合独立的 **agent-parliament plugin**——它包含一个 orchestrator 调度总纲 + 五个分工角色的 skill，在不同开发阶段介入，指导何时调用哪个工具：

| Skill | 立场 | 介入阶段 | 主力工具 |
|---|---|---|---|
| `orchestrator` | 路由 | 不确定用哪个角色时 | （调度层，不直接调工具） |
| `project-planner` | 想清楚 | 动工前需求/调研/架构 | `delegate_research`、`validate_approach`、`consensus` |
| `code-developer` | 造 | 方案落地、改 bug | `peer_review`、`test_audit`、`delegate_research` |
| `tester` | 攻（外部） | 高风险产出交付前 | `verify_implementation`、`peer_review`、`independent_analysis` |
| `untangler` | 看清 | 卡住、方向混沌 | `independent_analysis`、`consensus` |
| `memory-keeper` | 守 | 阶段归档、文档不一致 | `delegate_research`、`independent_analysis` |

> **tester 与 code-developer 构成"构建-验证"闭环**：developer 交付前用 peer_review 自审自己的 diff，tester 从外部用同样的工具攻击别人的 diff——调用主体变了，确认偏差就被打破。tester 内化了分层攻击清单、稳态假设、Mutation 视角，且不修复（修复回流 developer）。

plugin 的 skill 定义"遇到这种情况调哪个工具"（调度指南），角色人格由各客户端的 subagent 配置定义。两者配合。共享铁律：**MCP 工具用来交叉验证，不替代自己思考——先有自己的结论，再用工具印证或证伪。**

> 这套角色分工是可选的。你也可以在任意支持 MCP 的客户端里直接调用这 10 个工具。

---

## 项目结构

```
AgentParliament/
├── server.py              # MCP 接口层，10 个工具 handler
├── prompts.py             # prompt 纯函数 + 记忆块注入
├── chain.py               # delegate_chain 编排 + delegate_dialogue session + 工具调度器
├── render.py              # 结果渲染 + 结构校验
├── runner.py              # 执行层：BaseRunner/CLIRunner/APIRunner、失败链、熔断器
├── worktree_manager.py    # verify_implementation 的 git worktree 隔离环境管理
├── profiles.json          # 模型与角色配置（gitignore，含敏感 token）
├── profiles.example.json  # 配置模板
├── mcp.config.json        # MCP 客户端配置模板
├── pyproject.toml         # 项目元数据与依赖
└── docs/                  # ADR + 方案文档（gitignore）
```

---

## 常见问题

<details>
<summary><b>为什么同时有 CLI 和 API 两个后端？</b></summary>

CLI（Claude Code CLI）自带文件系统访问（Read/Grep/Glob）和只读沙箱，适合 Tier 1/2 需要读代码或跑测试的场景；API 直连（httpx）省去子进程启动开销，适合 Tier 0 纯文本融合任务（如 consensus 合成）。两者共享同一套失败链和熔断器，按场景自动路由，对调用方透明。

</details>

<details>
<summary><b>支持哪些模型？</b></summary>

任何兼容 Anthropic API 格式的端点均可。已验证：DeepSeek、GLM、Mimo、MiniMax、Claude Opus（作为强模型）、LongCat 等。自部署的 vLLM/TGI 端点只要实现 `/v1/messages` 接口即可。API 直连路径同时发送 `x-api-key` + `Authorization: Bearer`，兼容两类网关。

</details>

<details>
<summary><b>副 Agent 会修改我的文件吗？</b></summary>

- **Tier 0/1**：不会。只读白名单 + 写黑名单双重护栏，即使 prompt 被注入恶意指令也无法突破
- **Tier 2**（verify_implementation）：在 git worktree **隔离副本**内可写可执行，但主仓库工作树字节级不变（基于未提交改动的游离快照创建，独立临时索引）。子 Agent 改动以建议性 diff 交主 Agent 审批，是否合并由你决定

</details>

<details>
<summary><b>verify_implementation 的安全边界是什么？</b></summary>

worktree 隔离的是**文件树**，不是**操作系统进程**。子 Agent 在副本内执行的代码理论上仍可访问副本之外的文件系统、可联网——这是"运行不完全受控代码"的有意识取舍。安全靠三层兜底：(1) worktree 快照隔离保证主仓库零改动；(2) 工具黑名单（rm/git push/curl 等禁止）；(3) 建议性 diff 交主 Agent 审批。高风险项目应谨慎使用。

</details>

<details>
<summary><b>怎么排查 prompt 传参截断或模型调用失败？</b></summary>

**传参链路自检**：

```bash
uv run --directory /path/to/AgentParliament python runner.py --selfcheck
```

用一条含多行的探针 prompt 跑一次，确认 prompt 完整送达。自检失败会明确指出是模型调用失败还是末行未被返回（疑似截断）。

**调试日志**：设置 `AGENTPARLIAMENT_DEBUG=1` 后，每次子进程调用的真实结果（returncode、关键环境变量、stdout/stderr）写入 `logs/debug.log`，凭据字段已脱敏。

</details>

---

## License

MIT
