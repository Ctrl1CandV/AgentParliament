<div align="center">

# 🏛️ AgentParliament

**多模型协作的 MCP 服务——用多个国产模型逼近顶级模型的效果**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MCP Protocol](https://img.shields.io/badge/MCP-1.2+-6366F1)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

</div>

---

## 为什么需要 AgentParliament？

大部分开发者并不能频繁使用国外的顶级模型——Claude Opus、GPT-5 这类模型门槛高、成本贵。日常开发中，我们更多依赖 DeepSeek、GLM、Kimi 甚至是 Mimo 这样的国产模型。

必须承认，当前国产模型在思考的深度、广度和严谨性上，与顶级模型仍有差距。而且，**无论用什么模型，单模型输出都存在三个结构性问题**：

| 问题               | 表现                                               |
| ------------------ | -------------------------------------------------- |
| **盲区**     | 模型看不到自己遗漏了什么——它不知道自己不知道什么 |
| **确认偏差** | 模型倾向于肯定自己的结论，而非批判性审视           |
| **方案幻觉** | 模型设计的方案看似合理，实际落地时才发现不可行     |

**AgentParliament 的解法**：让多个模型扮演不同角色——审查者、反对者、独立审计者——从不同视角审视同一份代码或方案，交叉验证、互相纠错。**用多个国产模型的协作，逼近顶级模型的效果。**

---

## 它能做什么？

### 10 个专业工具，覆盖开发全流程

| 工具                           | 作用                                                                                          | 典型场景                                                                           |
| ------------------------------ | --------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| **delegate_research**    | 委托副模型做只读调研，回传结构化结论                                                          | 复杂问题需要第二视角，或想把调研从主会话剥离以保持上下文干净                       |
| **peer_review**          | 对 `git diff` 做分级代码审查（🔴严重/🟠重要/🟡次要/🟢建议）                                 | 完成实现后想要另一个模型独立核查；支持 `structured=True` 输出可程序化处理的 JSON |
| **independent_analysis** | 让第三方模型批判性审视已有结论，找盲区与逻辑漏洞                                              | 拿到结论后仍不确定、想验证可靠性                                                   |
| **consensus**            | 并行多模型回答同一问题，自动对比共识与分歧；可选 `synthesize=True` 让聚合模型融合出单一结论 | 关键决策需要多模型交叉验证（真正的「拼好模」）                                     |
| **validate_approach**    | 让模型扮演反对者，对架构/方案找漏洞                                                           | 方案实施前提前排雷                                                                 |
| **test_audit**           | 静态分析源码与测试，找出未覆盖的逻辑分支与边界                                                | 实现完功能后想知道「哪些情况还没测到」                                             |
| **advisor_analysis**     | 两阶段战略求助：中等模型先出草稿，不确定或高风险时才升级求助强模型                            | 想用便宜模型兜底、关键处再求助强模型（如 Claude Opus）                             |
| **delegate_chain**       | 多步推理链：主 Agent 定义思维链结构，server.py 依次执行；链内子 Agent 可全文件读取            | 重要决策需要完整链路深入思考（如调研->审视->验证），担心自己有盲区                 |
| **delegate_dialogue**    | 带对话能力的调研：子 Agent 可通过 `[ASK_PARENT]` 标记向主 Agent 提问，主 Agent 回答后继续    | 调研中存在方向性分歧、需主 Agent 拍板时                                           |
| **verify_implementation**| 在 git worktree 隔离副本中设计并执行测试，产出 ground truth（测试是否通过）+ 建议性 diff    | 想真正运行测试验证方案可行性，而非静态分析；改动不落盘主仓库，交主 Agent 审批合并  |

> 副 Agent 按三级能力阶梯运行：Tier 0（纯文本融合，走 API 直连）、Tier 1（只读探索，default+白名单+黑名单）、Tier 2（worktree 隔离可写可执行）。Tier 0/1 的副 Agent 永远不会修改你的文件；Tier 2（verify_implementation）在隔离副本内可写，但改动以建议性 diff 交主 Agent 审批，主仓库工作树保持干净。详见下文。

---

## 真实效果

口说无凭，我们造了一个「陷阱项目」`minibank-trap` 来实测：一个迷你银行账户系统，里面**故意埋了 6 个分级缺陷**（2 个致命、2 个重要、2 个次要），外加 5 个方案级设计漏洞和 2 条只有读了项目记忆才能发现的「项目专属规则」违背。标准答案私有保存、绝不喂给模型——模型得自己把雷挖出来。

结果是这样的：

| 拿什么考它            | 考了什么                                          | 成绩                                                                          |
| --------------------- | ------------------------------------------------- | ----------------------------------------------------------------------------- |
| `peer_review`       | 6 个已知代码缺陷能挖出几个                        | **6 / 6 全中**，每个都带行号和修复建议                                  |
| `validate_approach` | 5 个方案设计漏洞能拦下几个                        | **5 / 5 全拦**                                                          |
| 共享记忆注入          | 「计息口径」这类藏在 `CLAUDE.md` 里的项目硬约束 | 传了 `project_dir` 才稳定揪出违规；不传只会含糊反问                         |
| 战略求助              | 中等模型出错时，强模型能不能救场                  | Claude 不仅纠正了草稿里的技术错误，还补出了草稿**漏掉的最严重那个问题** |

一句话：**几个国产中等模型组队，把顶级模型才能稳定发现的问题挖了出来。**

> 成本与耗时：单次调用消耗 token 数因路径而异（走多模型合成/强模型求助的路径偏高，单模型审查在低位），单次耗时 **15–150 秒**（视是否触发多模型而定）。成本以 token 计数展示（端点无关），不再展示美元价格（不同用户用不同端点，美元无意义）。

---

## 核心特性

### 🔄 角色失败链——自动降级，永不空回

每个角色配置一条模型优先级链，首选模型不可用时自动降级到下一个，所有模型都失败才报错。

```json
"roles": {
  "researcher": ["deepseek", "glm", "minimax"],
  "third_party": ["glm", "deepseek", "mimo"],
  "reviewer": ["glm", "mimo", "deepseek", "minimax"],
  "aggregator": ["glm", "deepseek"],
  "strong_aggregator": ["claude", "glm"],
  "advisor": ["claude", "glm"]
}
```

### ⚡ 轻量熔断器——避免对挂掉的模型反复等满超时

进程内为每个模型维护连续失败计数：达到阈值（默认 3 次）后在冷却期（默认 5 分钟）内直接跳过该模型，不再等满超时。成功一次即清零计数。

- **只有明确的「模型不可用」类错误才计入熔断**：鉴权失败、额度/限流、模型 ID 无效、网络连接
- **超时和未知错误不计入**——超时可能是模型在认真思考，不代表「模型挂了」
- 仅在单个 MCP 服务进程生命周期内有效，重启自然清零，适合应对「某模型临时挂了几分钟」的场景

### 🧠 共享记忆注入——让副 Agent「懂这个项目」

每次调用前显式读取项目根目录的 `CLAUDE.md`（领域语言、架构决策、硬约束）并注入 prompt 开头，使副 Agent 确定性地获得项目上下文，而非依赖 Claude Code 是否自动加载。实测显示：传入 `project_dir` 后，副 Agent 才能稳定发现「违反项目专属规则」类缺陷（如计息口径、金额存储约定）。未传时结果开头会出现明确警告，提示记忆体未注入。

除 `CLAUDE.md` 外，还会增量注入两项轻量执行上下文（best-effort，文件不存在或读取失败都静默降级）：

- **SPEC.md 执行上下文**：从 `## 进度` 段取最后一条非空列表项、从 `## 下一步` 段取第一条非空行，拼成一行 `[执行进度] ... | [下一步] ...` 注入 prompt。让副 Agent 知道「当前做到哪、接下来做什么」，与提示词侧约定的三段式标题（`## 执行方案` / `## 进度` / `## 下一步`）匹配
- **ADR 索引**：优先从 `CLAUDE.md` 内容中提取 `[ADR-XXX]` 索引行（语义密度高）；无索引时 fallback 到扫描 `docs/adr/` 目录列文件名。让副 Agent 知道项目已有哪些架构决策可参考

三者共同构成「回读闭环」：副 Agent 既知道长期制度与硬约束（CLAUDE.md），也知道当前进度与下一步（SPEC.md），还知道有哪些已定架构决策（ADR 索引）。

### 🎯 战略求助——中等模型扛大头，关键处求助强模型

`advisor_analysis` 采用两阶段：中等模型先出草稿，仅在它自评不确定（输出 `[NEED_ADVISOR]`）或主 Agent 显式 `force_advisor=True` 时，才升级求助强模型。大多数调用只花一次中等模型的成本，关键决策才动用强模型——这是「逼近」而非「依赖」顶级模型。

### 🧩 多模型融合（拼好模）——consensus 合成

`consensus(synthesize=True)` 在并行收集各模型回答后，额外用聚合模型（`aggregator_role` 可配）批判性地裁决分歧、融合出单一高质量结论，同时保留各模型原始回答供追溯。日常用中等模型聚合，关键决策可指定强模型聚合。

### ⛓️ 多步推理链——delegate_chain

`delegate_chain` 是编排层工具，把多个原子工具串成一条思考链。主 Agent 定义链路结构（如调研→审视→验证），server.py 依次执行，每步输出自动成为下一步输入。链内子 Agent 可读取项目内全部文件，适合担心自己有盲区的重要决策。

`delegate_dialogue` 是带对话能力的调研工具。子 Agent 在调研中遇到需主 Agent 确认的方向时，可在结论末尾输出 `[ASK_PARENT]` 标记结束本轮；主 Agent 带 `session_id+answer` 再次调用，子 Agent 继续同一任务。`max_dialogue`（默认 3）限制最大轮次，TTL 兜底清理。

可选 `synthesize=True` 在最后加一步综合，融合各步结论为单一高质量输出。日常调研、审查、简单验证仍使用 7 个原子工具，重要决策用 delegate_chain，方向性分歧用 delegate_dialogue。

### 🔒 只读安全——副Agent无法改文件

所有副Agent都以只读方式运行，主Agent始终掌控执行权。根据工具特性采用两种只读策略：

- **peer_review / independent_analysis / consensus / validate_approach / test_audit / advisor_analysis**：使用 `--permission-mode plan`，Claude Code 内置禁止所有写操作，最严格的只读沙箱
- **delegate_research**：使用 `--permission-mode default` + 显式只读工具白名单（Read/Grep/Glob 及只读 Bash 子命令）+ 写操作工具黑名单（Write/Edit/Bash 等）双重护栏。原因：plan 模式的「提案后等待人类审批」语义会让 headless 单轮调研卡在「等待审阅」，调研结论被困在 plan 文件里无法返回；default 模式下用白名单限定只读，安全性等价且不会触发 halt

### 🌐 环境隔离——不受 ccswitch 串扰

每个模型分配独立的 `CLAUDE_CONFIG_DIR`，清理所有 `ANTHROPIC_*`/`CLAUDE_CODE_*` 前缀变量后再注入，彻底杜绝 settings.json 覆盖模型配置。

### 📡 按需放权——联网搜索与跨文件读取

- `delegate_research(allow_web=True)` — 放开 WebSearch/WebFetch，获取最新文档和版本特性
- `peer_review(context_files=[...])` — 放开文件读取，审查者看到跨文件交互而非孤立 diff

### 📊 结构化输出——可程序化处理

`peer_review(structured=True)` 返回 JSON 数组，每个 issue 含 severity/file/line/description/suggestion，可直接 `json.loads()` 提取统计。输出经校验（剥 markdown fence、字段完整性检查），坏 JSON 会被标注 `[格式校验失败]` 并保留原文供人工 salvage。

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
      "model": "glm-5.1"
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

> `defaults.role_overrides` 可按角色覆盖全局超时：审查类任务（reviewer）通常较快可设短，调研类（researcher）可能需要更长。不配置时所有角色共用 `timeout_seconds`。

> 任何兼容 Anthropic API 格式的端点均可使用（DeepSeek、GLM、Mimo、MiniMax、自部署 vLLM 等）。
> `aggregator`/`strong_aggregator`/`advisor` 三个角色分别供 `consensus` 合成与 `advisor_analysis` 求助使用。上面的示例仅用已定义的 `deepseek`/`glm` 两个模型保证开箱可跑；若你有顶级模型（如 Claude Opus），在 `models` 中加一个 `claude` 条目，再把它放进 `strong_aggregator`/`advisor` 链首，即可在关键决策时求助强模型。

### 3. 配置 MCP 客户端

将以下内容添加到你的 MCP 客户端配置（如 Trae 的 MCP 设置）：

```json
{
  "mcpServers": {
    "AgentParliament": {
      "command": "uv",
      "args": [
        "run", "--directory",
        "/path/to/AgentParliament",
        "agent-parliament"
      ]
    }
  }
}
```

### 4. 开始使用

在对话中直接调用即可：

```
请用 peer_review 审查我最近的代码改动，重点关注并发安全性
```

```
用 delegate_research 调研这个项目的依赖关系，允许联网搜索
```

```
用 test_audit 分析 runner.py 的测试覆盖情况
```

---

## 项目结构

```
AgentParliament/
├── server.py              # MCP 服务接口层，10 个工具 handler
├── prompts.py             # prompt 纯函数 + 记忆块注入
├── chain.py               # delegate_chain 编排 + delegate_dialogue session 管理 + 工具调度器
├── render.py              # 结果渲染 + 结构校验
├── runner.py              # 执行层：BaseRunner/CLIRunner/APIRunner、失败链、环境隔离、熔断器
├── worktree_manager.py    # verify_implementation 的 git worktree 隔离环境生命周期管理
├── profiles.json          # 模型与角色配置（gitignore，含敏感 token）
├── profiles.example.json  # 配置模板（含 role_overrides 按角色覆盖超时）
├── mcp.config.json        # MCP 客户端配置模板
├── pyproject.toml         # 项目元数据与依赖
├── uv.lock                # uv 依赖锁定文件
└── docs/                  # ADR（架构决策记录）+ 方案文档
```

---

## 配合 agent-parliament plugin 使用（推荐）

AgentParliament 本身是一个被调用的 MCP 工具；要让它发挥最大价值，推荐配合独立的 **agent-parliament plugin** 使用——它包含一个 orchestrator 调度总纲 + 五个分工角色的 skill（project-planner / code-developer / tester / untangler / memory-keeper），在不同开发阶段介入，指导何时调用本 MCP 的哪个工具来交叉验证。

| Skill | 角色 | 介入阶段 | 主要调用的工具 |
|---|---|---|---|
| `orchestrator` | 调度总纲 | 着手项目/较大功能，不确定用哪个角色 | （路由层，不直接调工具） |
| `project-planner` | 前期规划 | 动工前需求分析、调研、架构设计 | `delegate_research(allow_web=True)`、`validate_approach`、`consensus` |
| `code-developer` | 代码开发 | 把方案落地为代码、改 bug | `peer_review`、`validate_approach`、`test_audit`、`delegate_research` |
| `tester` | 攻防验证 | 从外部攻击已交付的产出 | `verify_implementation`、`peer_review`、`test_audit`、`independent_analysis` |
| `untangler` | 纾困复盘 | 卡住、反复修补无果、方向混沌 | `independent_analysis`、`consensus`、`delegate_research` |
| `memory-keeper` | 文档管理 | 阶段结束归档、文档与代码不一致 | `delegate_research`、`independent_analysis`、`peer_review` |

> **tester 与 code-developer 构成"构建-验证"闭环**：code-developer 交付前用 peer_review 自审自己的 diff，tester 从外部用同样的工具攻击别人的 diff——调用主体变了，确认偏差就被打破。tester 的核心是"攻击别人的产出而非自审"，内化了分层攻击清单、稳态假设、Mutation 视角，且不修复（修复回流 code-developer）。

> **delegate_chain** 和 **delegate_dialogue** 是跨角色工具：面对需要完整链路深入思考的重要决策时，可用 delegate_chain 将多个原子工具串成思考链；当调研中存在方向性分歧、需主 Agent 拍板时，可用 delegate_dialogue 进行多轮对话。日常场景仍使用上表中的原子工具。

plugin 的 skill 定义"遇到这种情况调哪个工具"（工具调度指南），角色人格（立场、信念、身份）由各客户端的 subagent / agent 配置定义。两者配合使用。六个 skill 共享一条铁律：**MCP 工具用来交叉验证、不替代自己思考——先有自己的结论，再用工具印证或证伪。** 同时都会在工作前读取项目根目录的 `CLAUDE.md` 共享记忆体，与本 MCP 的记忆注入机制形成闭环。

> 这套角色分工是可选的。你也可以在任意支持 MCP 的客户端里直接调用这 10 个工具，不依赖 plugin。

---

## 常见问题

<details>
<summary><b>为什么用 Claude Code CLI 而不是直接调 API？</b></summary>

CLI 自带文件系统访问能力（Read/Grep/Glob），副Agent可以直接读代码库做深度分析，不需要主Agent手动拼接文件内容到 prompt。同时 CLI 的 `--permission-mode plan` / `default` + 工具白名单提供了开箱即用的只读沙箱，比自建 API 调用层更安全。

</details>

<details>
<summary><b>支持哪些模型？</b></summary>

任何兼容 Anthropic API 格式的端点均可。已验证：DeepSeek、GLM（火山引擎）、Mimo、MiniMax、Claude Opus（作为 `advisor`/`strong_aggregator` 的强模型）。自部署的 vLLM/TGI 端点只要实现 `/v1/messages` 接口即可使用。

</details>

<details>
<summary><b>副Agent会修改我的文件吗？</b></summary>

不会。审查/分析类工具以 `--permission-mode plan` 运行，Claude Code 在此模式下内置禁止所有写操作；`delegate_research` 以 `--permission-mode default` 运行，但用显式只读工具白名单（Read/Grep/Glob 等）+ 写操作黑名单（Write/Edit/Bash 等）双重护栏约束只读边界。即使 prompt 被注入恶意指令，也无法突破这些限制。

</details>

<details>
<summary><b>Windows 上有什么已知限制？</b></summary>

Claude Code 的 OS 级沙箱（Seatbelt/bubblewrap）在 Windows 原生环境下不可用。因此 `test_audit` 只做静态分析不运行测试，这是有意为之的安全权衡。如需执行测试，建议在 WSL2 环境下使用。

</details>

<details>
<summary><b>怎么排查 prompt 传参截断或模型调用失败？</b></summary>

项目内置两个诊断手段：

**传参链路自检**——用一条含多行的探针 prompt 跑一次，确认 prompt 完整送达、末行能被模型原样返回：

```bash
uv run --directory /path/to/AgentParliament python runner.py --selfcheck
```

自检通过表示从 `profiles.json` 读取、环境变量隔离、子进程启动到 prompt 传参的整条链路正常。自检失败会明确指出是探针模型调用失败还是末行未被返回（疑似传参截断）。

**调试日志**——设置环境变量 `AGENTPARLIAMENT_DEBUG=1` 后，每次子进程调用的真实结果（returncode、关键环境变量、stdout/stderr 前 800 字符）会写入项目目录下的 `logs/debug.log`，用于排查环境串扰、模型 ID 无效、鉴权失败等问题。`mcp.config.json` 模板默认已开启此变量。

</details>

---

## License

MIT
