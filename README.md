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

### 6 个专业工具，覆盖开发全流程

<table>
<tr>
<td width="50%">

---

## 真实效果

基于含 7 个已知缺陷的评测项目的实测数据：

| 指标           | 无工具辅助       | AgentParliament 辅助                                 | 提升           |
| -------------- | ---------------- | ---------------------------------------------------- | -------------- |
| 缺陷发现量     | 5-8 个（表面级） | **34 个**（含行号+修复建议）                   | **4-5x** |
| 已知缺陷命中率 | ~40%             | **100%**（test_audit 单工具）                  | —             |
| 否决错误方案   | 无               | **1 次**（PriorityQueue 方案因命名冲突不可行） | —             |
| 严重度升级     | 无               | **1 次**（非原子写入 major→critical）         | —             |

> 单次调用成本 $0.08-$0.32 | 单缺陷成本 $0.038 | 单次耗时 15-35 秒

---

## 核心特性

### 🔄 角色失败链——自动降级，永不空回

每个角色配置一条模型优先级链，首选模型不可用时自动降级到下一个，所有模型都失败才报错。

```json
"roles": {
  "reviewer": ["glm", "mimo", "deepseek"],
  "researcher": ["deepseek", "glm"],
  "third_party": ["glm", "deepseek", "mimo"]
}
```

### 🔒 只读安全——副Agent无法改文件

所有副Agent以 `--permission-mode plan` 运行，Claude Code 内置禁止所有写操作。主Agent始终掌控执行权。

### 🌐 环境隔离——不受 ccswitch 串扰

每个模型分配独立的 `CLAUDE_CONFIG_DIR`，清理所有 `ANTHROPIC_*`/`CLAUDE_CODE_*` 前缀变量后再注入，彻底杜绝 settings.json 覆盖模型配置。

### 📡 按需放权——联网搜索与跨文件读取

- `delegate_research(allow_web=True)` — 放开 WebSearch/WebFetch，获取最新文档和版本特性
- `peer_review(context_files=[...])` — 放开文件读取，审查者看到跨文件交互而非孤立 diff

### 📊 结构化输出——可程序化处理

`peer_review(structured=True)` 返回 JSON 数组，每个 issue 含 severity/file/line/description/suggestion，可直接 `json.loads()` 提取统计。

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
  "defaults": { "timeout_seconds": 300 },
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
    "reviewer": ["glm", "deepseek"]
  }
}
```

> 任何兼容 Anthropic API 格式的端点均可使用（DeepSeek、GLM、Mimo、自部署 vLLM 等）。

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
├── server.py          # MCP 服务入口，6 个工具定义
├── runner.py          # 核心执行层，子进程管理、失败链、环境隔离
├── profiles.json      # 模型与角色配置（gitignore，含敏感 token）
├── mcp.config.json    # MCP 客户端配置模板
├── pyproject.toml     # 项目元数据与依赖
└── sandbox/           # 评测产物（gitignore）
```

---

## 常见问题

<details>
<summary><b>为什么用 Claude Code CLI 而不是直接调 API？</b></summary>

CLI 自带文件系统访问能力（Read/Grep/Glob），副Agent可以直接读代码库做深度分析，不需要主Agent手动拼接文件内容到 prompt。同时 CLI 的 `--permission-mode plan` 提供了开箱即用的只读沙箱，比自建 API 调用层更安全。

</details>

<details>
<summary><b>支持哪些模型？</b></summary>

任何兼容 Anthropic API 格式的端点均可。已验证：DeepSeek、GLM（火山引擎）、Mimo。自部署的 vLLM/TGI 端点只要实现 `/v1/messages` 接口即可使用。

</details>

<details>
<summary><b>副Agent会修改我的文件吗？</b></summary>

不会。所有副Agent以 `--permission-mode plan` 运行，Claude Code 在此模式下内置禁止所有写操作。即使 prompt 被注入恶意指令，也无法突破 plan 模式的限制。

</details>

<details>
<summary><b>Windows 上有什么已知限制？</b></summary>

Claude Code 的 OS 级沙箱（Seatbelt/bubblewrap）在 Windows 原生环境下不可用。因此 `test_audit` 只做静态分析不运行测试，这是有意为之的安全权衡。如需执行测试，建议在 WSL2 环境下使用。

</details>

---

## License

MIT
