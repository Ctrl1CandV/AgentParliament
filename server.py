"""
server.py——AgentParliament的MCP入口

通过stdio暴露7个只读工具给主Agent：
1. delegate_research   —— 调研：把问题与相关文件交给副模型调研，回传结论
2. peer_review         —— 代码审查：把git diff交给副模型审查，回传分级问题
3. independent_analysis—— 独立分析：让第三方模型批判性审视已有结论，找盲区与漏洞
4. consensus           —— 多模型共识：并行让多个模型回答同一问题，自动对比共识与分歧；可开启synthesize让合成模型融合出单一结论
5. validate_approach   —— 方案验证：让另一个模型扮演反对者，对架构/方案找漏洞
6. test_audit          —— 测试审计：静态分析源码与测试，找出未覆盖的逻辑分支与边界
7. advisor_analysis    —— 战略求助：中等模型先出草稿，不确定或高风险时条件升级求助强模型

所有工具的副Agent都以只读模式运行，不会修改任何文件；改文件的动作始终留在主进程，由用户审阅后执行
- peer_review / independent_analysis / consensus / validate_approach / test_audit / advisor_analysis：
  使用 --permission-mode plan（Claude Code内置禁止所有写操作）
- delegate_research：使用 --permission-mode default + 显式只读工具白名单 + 写操作黑名单
  （plan模式的"提案后等待人类审批"语义会让headless单轮调研卡在"等待审阅"，故改用default模式）

共享记忆：每个工具调用前会显式读取副Agent工作目录下的CLAUDE.md（项目共享记忆体），
注入到prompt开头，使副Agent确定性地获得项目的领域语言、已定架构决策与硬约束，
而不依赖Claude Code在headless模式下是否自动加载CLAUDE.md。记忆体缺失或读取失败时静默降级，不影响工具主流程。
未传project_dir时在返回结果开头标注警告，提示记忆体未注入，避免主Agent在不知情下使用缺少项目上下文的结论。
"""

from __future__ import annotations

from runner import (
    Config,
    ParallelResult,
    ProfileError,
    RunResult,
    load_config,
    run_with_chain,
    run_parallel,
)
from mcp.server.fastmcp import FastMCP
from pathlib import Path
import json
import sys
mcp = FastMCP("AgentParliament")

# prompt改由stdin传入后已无命令行长度限制，此处仅保留一个宽松上限防止极端输入耗尽内存
_DIFF_SIZE_LIMIT = 200000

# 副Agent以只读plan模式运行，无法写文件。明确告知以抑制幻觉回复
_READONLY_NOTICE = (
    "\n\n[重要提醒] 你处于只读plan模式，无法创建、修改或删除任何文件。"
    "不要在回复中声称已创建文件或已写入内容，直接输出你的分析文本即可。"
)

# 审查/分析类工具的全部材料已随prompt给出，约束副Agent不要跑去读cwd里的文件而忽略prompt内容
_MATERIAL_NOTICE = (
    "[约束] 全部待审查/分析的材料已在下方完整给出，"
    "请直接基于下方内容工作，无需也不要读取任何文件。\n\n"
)

"""
配置采用延迟加载加缓存的策略：
- 避免import server时立刻执行IO，导致MCP客户端只看到"服务未启动"而看不到根因
- 首次工具调用时再读profiles.json，失败时把详细错误同时写入stderr与返回值
"""
_CONFIG_CACHE: Config | None = None

def _get_config() -> Config:
    """ 首次调用时加载配置；后续调用直接复用 """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None:
        try:
            _CONFIG_CACHE = load_config()
        except ProfileError as exc:
            # 同时写stderr，方便在Trae等宿主的MCP日志里立刻看到根因
            print(f"[AgentParliament]配置加载失败：{exc}", file=sys.stderr)
            raise
    return _CONFIG_CACHE

def _format_result(result: RunResult) -> str:
    """ 把RunResult渲染成给主Agent阅读的文本，并诚实标注降级情况与成本 """
    if not result.ok:
        lines = ["[AgentParliament]所有候选模型均失败，未能完成任务。", "尝试记录："]
        for attempt in result.attempts:
            error_detail = attempt.error or "（无错误详情）"
            # error_type 作为独立标注，便于主Agent机器可读地判断失败原因类型（鉴权/限流/网络等）
            type_tag = f"[{attempt.error_type}] " if attempt.error_type else ""
            lines.append(f"  - {attempt.model_name}: {type_tag}{error_detail}")
        return "\n".join(lines)

    header = f"[AgentParliament]由模型`{result.model_used}`完成。"
    if result.degraded:
        failed = "、".join(
            attempt.model_name for attempt in result.attempts if not attempt.ok
        )
        header += (
            f"\n注意：首选模型{failed}不可用，已降级到兜底模型。"
            f"若你需要的是独立第三方视角，请知悉本结果的模型多样性已打折扣。"
        )
    if result.total_cost_usd > 0:
        header += f"\n本次调用成本：${result.total_cost_usd:.4f}"
    return f"{header}\n\n{result.text}"

def _format_parallel_result(parallel: ParallelResult) -> str:
    """ 把ParallelResult渲染成给主Agent阅读的文本，含各模型结果与成本汇总 """
    if parallel.all_failed:
        lines = ["[AgentParliament]所有模型均失败，未能完成任务。"]
        for r in parallel.results:
            lines.append(f"  - {r.model_name}: {r.error}")
        return "\n".join(lines)

    header = f"[AgentParliament]{len(parallel.successful)}/{len(parallel.results)}个模型成功完成。"
    if parallel.total_cost_usd > 0:
        header += f" 总成本：${parallel.total_cost_usd:.4f}"
    return f"{header}\n\n{parallel}"

def _resolve_cwd(project_dir: str | None) -> str:
    """
    确定副Agent的工作目录，默认用MCP进程所在目录，若主Agent传入project_dir，会校验：
    1. 解析为绝对路径，防止`..`之类的路径穿越被透传到子进程
    2. 必须是已存在的目录，否则直接拒绝调用而不是让claude子进程产生模糊的报错
    """
    if not project_dir:
        return str(Path(__file__).parent)

    resolved = Path(project_dir).expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ProfileError(f"project_dir不存在或不是目录：{resolved}")
    return str(resolved)

# 共享记忆体文件名。副Agent通过MCP显式读取并注入，从而确定性地获得项目的领域语言、
# 已定架构决策与硬约束，而不依赖Claude Code在headless（-p）模式下是否会自动加载CLAUDE.md
_MEMORY_FILENAME = "CLAUDE.md"

# 注入记忆体的字符上限，防止异常膨胀的记忆体挤占prompt预算；超出时截断并提示
_MEMORY_CHAR_LIMIT = 16000

def _memory_block(cwd: str) -> str:
    """
    从副Agent工作目录读取共享记忆体（CLAUDE.md），渲染成注入prompt开头的记忆块
    设计要点：
    - 显式读取而非依赖Claude Code自动加载，确保被调用的副Agent确定性地获得项目记忆
    - 记忆体是best-effort：文件不存在或读取失败都返回空串，绝不让记忆问题阻断工具主流程
    - 超过字符上限时截断，避免异常膨胀的记忆体挤占prompt
    返回值要么是空串，要么是带引导语和分隔符的完整记忆块
    """
    memory_path = Path(cwd) / _MEMORY_FILENAME
    try:
        if not memory_path.is_file():
            return ""
        content = memory_path.read_text(encoding="utf-8").strip()
    except Exception:
        # 读取失败（权限、编码、IO等）不应影响工具调用，静默降级为"无记忆"
        return ""

    if not content:
        return ""

    if len(content) > _MEMORY_CHAR_LIMIT:
        content = (
            content[:_MEMORY_CHAR_LIMIT]
            + "\n…（项目记忆超过长度上限已截断，完整内容见项目CLAUDE.md）"
        )

    return (
        "[项目记忆] 以下是本项目的共享记忆体（CLAUDE.md），"
        "包含领域语言、已定架构决策、硬约束与当前状态。\n"
        "请在工作时沿用其中的术语、遵守其中的约束，不要与已定决策冲突；"
        "若你的分析与记忆体存在矛盾，请明确指出而非默默忽略。\n"
        "————（项目记忆开始）————\n"
        f"{content}\n"
        "————（项目记忆结束）————\n\n"
    )

def _project_dir_warning(project_dir: str | None) -> str:
    """
    project_dir 未传时返回警告，提示记忆体未注入；已传则返回空串。
    设计要点：
    - 不强制报错（调试 AgentParliament 自身时不传 project_dir 是合理的）
    - 不改 _memory_block 的 best-effort 语义
    - 只在结果 header 加一条可见警告，让主Agent感知到「这次调用没带记忆体」
    """
    if project_dir:
        return ""
    return (
        "[AgentParliament]⚠️未传 project_dir，本次未读取目标项目 CLAUDE.md 记忆体，"
        "副Agent缺少领域语言/架构决策/硬约束上下文。仅调试 AgentParliament 自身时可忽略。\n\n"
    )

# structured review 输出中每个问题对象必须包含的字段
_REQUIRED_REVIEW_KEYS = {"severity", "file", "line", "description", "suggestion"}

def _validate_structured_review(text: str) -> tuple[bool, str, str]:
    """
    尽力解析 peer_review(structured=True) 的输出。
    返回 (ok, cleaned_json_or_raw, message)：
    - ok=True：解析成功，cleaned_json 为干净 JSON 数组文本
    - ok=False：解析失败，cleaned_json 为原始文本（供主Agent人工 salvage），message 为失败原因
    设计取舍：不自动重试（run_with_chain 已跑完整条链，重试成本翻倍）；
    解析失败时仍返回原始文本，不隐藏失败信号
    """
    raw = text.strip()

    # 剥 markdown fence：模型有时会把 JSON 包在 ```json ... ``` 里
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        arr = json.loads(raw)
    except json.JSONDecodeError as exc:
        return False, text, f"[格式校验失败]无法解析为JSON：{exc}"

    if not isinstance(arr, list):
        return False, text, "[格式校验失败]顶层不是数组"

    for i, item in enumerate(arr):
        if not isinstance(item, dict):
            return False, text, f"[格式校验失败]第{i}项不是对象"
        missing = _REQUIRED_REVIEW_KEYS - item.keys()
        if missing:
            return False, text, f"[格式校验失败]第{i}项缺字段：{missing}"

    return True, json.dumps(arr, ensure_ascii=False), ""

# delegate_research 使用的只读工具白名单（配合 default 权限模式）
# 不再依赖 plan 模式，因为 plan 模式的"提案后等待人类审批"语义会导致 headless 单轮调研卡死
# default 模式下用 allowed_tools 显式限定可调用工具；为防 CLI 版本差异导致默认权限破防，
# 同时用 disallowed_tools 黑名单兜底，双重护栏
_RESEARCH_TOOLS_LOCAL = [
    "Read", "Grep", "Glob",
    # 只读 Bash 子命令：plan 模式原本允许这些调研利器，切到 default 后需显式列出以免能力回归
    "Bash(git log:*)", "Bash(git show:*)", "Bash(git blame:*)", "Bash(git diff:*)",
    "Bash(ls:*)", "Bash(dir:*)", "Bash(find:*)", "Bash(wc:*)", "Bash(cat:*)",
]
_RESEARCH_TOOLS_WEB = list(_RESEARCH_TOOLS_LOCAL) + ["WebSearch", "WebFetch"]

# 写操作工具黑名单：无论 allowed_tools 如何配置，这些工具一律拒绝
# 防御 default 模式下 CLI 版本差异导致的权限边界漂移
_DISALLOWED_TOOLS = ["Write", "Edit", "NotebookEdit", "Bash"]

# 压制 plan/default 模式下副Agent"停下来等待审批"的行为
# plan 模式的 propose-and-halt 语义是 Claude Code 系统级指令，仅靠 prompt 无法 100% 压制，
# 故 delegate_research 已切换为 default 模式；此指令作为双保险，进一步明确要求直接输出结论
_NO_HALT_NOTICE = (
    "\n\n[执行要求] 请一次性完成全部调研并直接在回复中输出最终结论。"
    "不要提交计划等待审批，不要中途停下来询问是否继续，"
    "不要输出『等待审阅』或『请确认』之类的话。"
)

# 工具 1：delegate_research
@mcp.tool()
def delegate_research(
    question: str, role: str = "researcher",
    project_dir: str | None = None,
    context_files: list[str] | None = None,
    allow_web: bool = False,
) -> str:
    """
    委托一个只读副Agent对代码库做调研，并返回它的结论
    适用场景：问题较复杂、需要第二个模型的视角，或想把调研从主会话剥离以保持主上下文干净
    注意副Agent只读本地代码，不会改文件

    [使用前提] 本工具用于补充和交叉验证你自己的调研，而非替代它。
    调用前应先自己形成初步判断或做基础调研，再带着具体问题/假设来委托；
    否则只是把思考原样转给可能更弱的副模型，效果反而比你亲自调研更差。

    Args:
        question: 要副Agent回答的调研问题，应尽量具体
        role: 使用profiles.json中的哪条角色失败链，默认"researcher"
        project_dir: 副Agent的工作目录，必须传入当前项目根目录，以便副Agent读取该项目的CLAUDE.md共享记忆与docs/adr决策记录。不传则退回MCP所在目录，仅适合调试AgentParliament自身
        context_files: 可选，主Agent已定位的相关文件路径列表。会拼进提示词，引导副Agent优先阅读这些文件，避免从头扫描整个项目造成浪费
        allow_web: 可选，是否允许副Agent联网搜索（WebSearch/WebFetch）。默认False（仅本地代码调研）。
            当调研问题涉及最新文档、版本特性、外部资料时设为True；副Agent仍只读，联网仅用于获取信息，不会改文件

    Returns:
        副Agent的调研结论文本；若发生降级会在开头注明
    """
    prompt = question
    if context_files:
        joined = "\n".join(f"- {p}" for p in context_files)
        prompt = (
            f"{question}\n\n"
            f"请优先阅读以下相关文件，无需扫描整个项目：\n{joined}"
        )

    # 引导副Agent结构化输出，避免漫无目的的浏览或泛泛而谈
    prompt += (
        "\n\n请按以下结构输出调研结论：\n"
        "1.【问题复述】用自己的话复述调研问题，确认理解无误\n"
        "2.【发现】列出你的调研发现，按重要性排序\n"
        "3.【依据】每条发现附上支撑证据（代码位置、文档引用等）\n"
        "4.【结论】对原始问题的直接回答\n"
        "5.【局限】本次调研可能遗漏的方面"
    )
    # 单独追加：即使上方结构化指令被重构，no-halt 指令仍保留
    prompt += _NO_HALT_NOTICE

    # 用 default 模式 + 显式只读工具白名单替代 plan 模式
    # 原因：plan 模式的"调研→写计划文件→等待人类审批"语义会让 headless 单轮调研卡在"等待审阅"，
    # 调研结论被困在 plan 文件里无法返回；default 模式下用 allowed_tools 限定只读，安全性等价且不会触发 halt
    # disallowed_tools 作为黑名单兜底，防 default 模式下 CLI 版本差异导致默认权限破防
    allowed_tools = list(_RESEARCH_TOOLS_WEB if allow_web else _RESEARCH_TOOLS_LOCAL)

    try:
        cwd = _resolve_cwd(project_dir)
        result = run_with_chain(
            config=_get_config(),
            role=role,
            prompt=_memory_block(cwd) + prompt,
            cwd=cwd,
            allowed_tools=allowed_tools,
            permission_mode="default",
            disallowed_tools=_DISALLOWED_TOOLS,
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    return _project_dir_warning(project_dir) + _format_result(result)

# 工具 2：peer_review
@mcp.tool()
def peer_review(
    diff: str, role: str = "reviewer",
    focus: str = "", project_dir: str | None = None,
    context_files: list[str] | None = None,
    structured: bool = False,
) -> str:
    """
    委托一个只读副Agent审查一段代码改动，返回按严重程度分级的问题列表
    适用场景：主Agent完成实现后，想要另一个模型独立核查。审查只需要diff， token占用远小于全量文件。副Agent只读，不会改文件。

    Args:
        diff: 待审查的改动，通常是`git diff`的输出。大小限制约200KB，超过请改用文件路径
        role: 使用哪条角色失败链，默认"reviewer"
        focus: 可选，审查重点（如"安全性""并发""边界条件"），留空则做全面审查
        project_dir: 副Agent的工作目录，必须传入当前项目根目录，以便副Agent读取该项目的CLAUDE.md共享记忆与docs/adr决策记录；不传则退回MCP所在目录，仅适合调试AgentParliament自身
        context_files: 可选，与本次改动相关的文件路径列表，如被改函数的调用方、接口定义
            传入后副Agent会阅读这些文件以理解跨文件交互，避免纯diff审查只见局部、看不到接口契约与调用方影响的盲区
        structured: 可选，是否要求副Agent以JSON结构输出审查结果。默认False（人类可读的分级文本）。
            设为True时返回JSON数组，每项含severity/file/line/description/suggestion字段，便于主Agent程序化提取问题数量与严重程度

    Returns:
        分级的审查结论文本；若发生降级会在开头注明。structured=True时result部分为JSON数组文本
    """
    if len(diff) > _DIFF_SIZE_LIMIT:
        return (
            f"[AgentParliament]diff大小为 {len(diff)} 字符，"
            f"超过安全上限{_DIFF_SIZE_LIMIT}。"
            f"请将diff写入临时文件后通过context_files参数传入文件路径，"
            f"或只传关键改动的diff。"
        )

    # 默认约束副Agent只看diff不读文件；一旦传入context_files，则改为引导它阅读这些上下文文件，
    # 并把文件所在目录通过--add-dir放开，确保Read能访问cwd之外的路径
    prefix = _MATERIAL_NOTICE
    extra_dirs: list[str] | None = None
    if context_files:
        joined = "\n".join(f"- {p}" for p in context_files)
        prefix = (
            "[上下文]以下文件与本次改动相关，请在审查时阅读它们，"
            "理解跨文件交互、接口契约与调用方影响，不要孤立地只看diff：\n"
            f"{joined}\n\n"
        )
        extra_dirs = sorted({
            str(Path(p).expanduser().resolve().parent) for p in context_files
        })

    focus_line = f"本次审查重点：{focus}\n\n" if focus else ""
    if structured:
        instruction = (
            "你是一名严谨的代码审查者。请审查下面的代码改动，并以JSON数组格式输出所有问题。\n\n"
            "审查流程（前两步仅在心里完成，不要输出）：\n"
            "1. 先在心里理解本次改动的意图和范围，确认你理解了改动要做什么\n"
            "2. 逐项检查改动的正确性、安全性和可维护性\n"
            "3. 最终只输出JSON数组本身，不要包裹markdown代码块，不要添加任何解释文字\n\n"
            "数组每个元素是一个对象，包含以下字段：\n"
            '  - severity: 严重程度，取值"critical"/"major"/"minor"/"suggestion"之一\n'
            "  - file: 问题所在文件（无法确定时用空字符串）\n"
            "  - line: 问题所在行号或行号范围字符串（无法确定时用空字符串）\n"
            "  - description: 问题描述\n"
            "  - suggestion: 修复建议\n"
            "若改动无明显问题，输出空数组[]。\n\n"
        )
    else:
        instruction = (
            "你是一名严谨的代码审查者。请审查下面的代码改动。\n\n"
            "审查流程：\n"
            "1. 先用1-2句话复述本次改动的意图和范围，确认你理解了改动要做什么\n"
            "2. 逐项检查改动的正确性、安全性和可维护性\n"
            "3. 按严重程度分级列出问题：🔴 严重 / 🟠 重要 / 🟡 次要 / 🟢 建议\n\n"
            "对每个问题指出所在位置、原因和修复建议。若改动无明显问题也请明确说明。\n"
            "注意：不要只夸不改，审查的价值在于发现问题而非肯定改动。\n\n"
        )
    prompt = (
        f"{prefix}"
        f"{instruction}"
        f"{focus_line}"
        f"待审查的改动（diff）：\n```diff\n{diff}\n```"
        f"{_READONLY_NOTICE}"
    )

    try:
        cwd = _resolve_cwd(project_dir)
        result = run_with_chain(
            config=_get_config(),
            role=role,
            prompt=_memory_block(cwd) + prompt,
            cwd=cwd,
            extra_dirs=extra_dirs,
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    # structured=True 时对模型输出做尽力解析：剥 markdown fence、json.loads、校验必填字段。
    # 解析失败不重试（run_with_chain 已跑完整条链），但明确标注失败并保留原始输出供主Agent salvage。
    # 整链失败时 result.text 为空，直接走失败渲染，避免误报"无法解析为JSON"掩盖真实失败原因。
    if structured and result.ok:
        ok, cleaned, msg = _validate_structured_review(result.text)
        # _format_result 的 header 与正文之间用 "\n\n" 分隔，取 header 部分复用
        rendered = _format_result(result)
        header, _, body = rendered.partition("\n\n")
        if not ok:
            return (
                _project_dir_warning(project_dir)
                + header + "\n" + msg + "\n\n原始输出：\n" + body
            )
        # 校验通过：用干净 JSON 替换正文
        return _project_dir_warning(project_dir) + header + "\n\n" + cleaned

    return _project_dir_warning(project_dir) + _format_result(result)

# 工具 3：independent_analysis
@mcp.tool()
def independent_analysis(
    original_task: str,
    existing_result: str,
    concern: str = "",
    role: str = "third_party",
    project_dir: str | None = None,
) -> str:
    """
    让第三方模型批判性地审视已有结论，找出盲区、逻辑漏洞或过度推断
    适用场景：主Agent拿到调研或审查结果后，仍觉得不确定或想验证可靠性时调用
    返回的是结构化的共识与分歧对比，而非简单地换模型重做一遍

    Args:
        original_task: 原始问题/任务是什么
        existing_result: 主Agent已经拿到的结论
        concern: 可选，主Agent具体担心什么（如"是否遗漏了边界条件""性能评估是否准确"），帮助副Agent聚焦审查
        role: 使用哪条角色失败链，默认"third_party"，且优先用与主Agent不同的模型以获得独立视角
        project_dir: 副Agent的工作目录，必须传入当前项目根目录，以便副Agent读取该项目的CLAUDE.md共享记忆与docs/adr决策记录；不传则退回MCP所在目录，仅适合调试AgentParliament自身

    Returns:
        结构化的独立分析报告，含共识、分歧与替代判断。
    """
    concern_block = ""
    if concern:
        concern_block = f"\n主Agent的具体关注点：{concern}\n请针对此关注点重点审查。"

    prompt = (
        f"{_MATERIAL_NOTICE}"
        "你是一名独立审计者，你的职责是批判性地审视另一个模型给出的结论，"
        "找出其中可能存在的逻辑漏洞、盲区或过度推断。\n\n"
        "分析流程：\n"
        "1. 先用自己的话复述原结论的核心观点，确保你准确理解了原结论，而非攻击稻草人\n"
        "2. 逐项审视原结论的推理链，检查每一步是否有跳跃、遗漏或过度推断\n"
        "3. 找出原结论可能遗漏的重要维度\n\n"
        "请按以下结构输出：\n"
        "1.【结论概要】用1-2句话概括原结论的核心观点\n"
        "2.【共识】列出你与原结论一致的部分\n"
        "3.【分歧】列出你与原结论不一致的部分，对每个分歧说明：\n"
        "   - 原结论说了什么\n"
        "   - 你为什么认为这可能有问题\n"
        "   - 你的替代判断是什么\n"
        "4.【盲区】原结论可能遗漏了哪些重要维度\n"
        "5.【总体评价】原结论整体是否可靠（可靠/部分可靠/不可靠），以及你的理由\n\n"
        "注意：你的目标是找出原结论的薄弱环节，而非全盘否定。如果原结论整体可靠，请诚实说明。\n\n"
        f"原始任务：{original_task}\n\n"
        f"已有结论：{existing_result}"
        f"{concern_block}"
        f"{_READONLY_NOTICE}"
    )

    try:
        cwd = _resolve_cwd(project_dir)
        result = run_with_chain(
            config=_get_config(),
            role=role,
            prompt=_memory_block(cwd) + prompt,
            cwd=cwd,
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    return _project_dir_warning(project_dir) + _format_result(result)

# 工具 4：consensus
@mcp.tool()
def consensus(
    question: str,
    role: str = "third_party",
    project_dir: str | None = None,
    context_files: list[str] | None = None,
    synthesize: bool = False,
    aggregator_role: str = "reviewer",
) -> str:
    """
    并行让多个模型独立回答同一问题，然后自动对比共识与分歧
    适用场景：关键决策时需要多模型交叉验证，对应"两个Agent同时调研然后综合"的工作流
    所有模型同时执行，互不等待，互不影响

    [使用前提] 本工具用多模型的独立视角交叉验证你的判断，而非替你做判断。
    调用前你应已对问题形成自己的初步结论，再用本工具印证或证伪；
    最终的综合判断仍由你负责，不要把多模型的回答当作免思考的现成答案。

    Args:
        question: 要多模型回答的问题，应尽量具体
        role: 使用哪条角色链上的模型，默认"third_party"
        project_dir: 副Agent的工作目录，必须传入当前项目根目录，以便副Agent读取该项目的CLAUDE.md共享记忆与docs/adr决策记录；不传则退回MCP所在目录，仅适合调试AgentParliament自身
        context_files: 可选，主Agent已定位的相关文件路径列表
        synthesize: 可选，是否在收集各模型回答后再跑一次合成模型，输出单一综合结论。默认False（各模型回答原样拼接，由主Agent自己综合）。
            设为True时额外用aggregator_role指定的角色链跑一次合成，成本增加一次模型调用，但能真正deliver"拼好模"——把多个中等模型的视角融合成一个高质量结论。
            日常建议用中等模型合成（aggregator_role="aggregator"），关键决策可用强模型合成（aggregator_role="strong_aggregator"）
        aggregator_role: 可选，合成用的角色失败链，默认"reviewer"。
            在profiles.json中配置"aggregator"(中等模型)和"strong_aggregator"(含claude等强模型)两种角色，按场景选用

    Returns:
        多模型回答的对比报告。
        synthesize=False：含各模型独立回答，由主Agent自行对比共识与分歧。
        synthesize=True：先给单一合成结论，再附各模型原始回答（保留分歧信号，便于主Agent追溯）。
    """
    prompt = question
    if context_files:
        joined = "\n".join(f"- {p}" for p in context_files)
        prompt = (
            f"{question}\n\n"
            f"请优先阅读以下相关文件，无需扫描整个项目：\n{joined}"
        )

    # 引导各模型结构化输出，便于主Agent对比共识与分歧
    prompt += (
        "\n\n请按以下结构输出你的独立判断：\n"
        "1.【问题复述】用自己的话复述问题，确认理解无误\n"
        "2.【核心观点】你对问题的直接回答（是/否/视情况而定，并说明理由）\n"
        "3.【关键论据】支撑你观点的主要依据（按重要性排序）\n"
        "4.【不确定之处】你不确定或缺乏足够信息判断的方面\n"
        "5.【替代视角】你可能忽略了的其他合理观点"
    )

    try:
        cwd = _resolve_cwd(project_dir)
        parallel = run_parallel(
            config=_get_config(),
            role=role,
            prompt=_memory_block(cwd) + prompt,
            cwd=cwd,
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    if parallel.all_failed:
        return _project_dir_warning(project_dir) + _format_parallel_result(parallel)

    # 手动汇总：把各模型结果拼在一起，让主Agent看到对比，主Agent自己就是最好的综合者
    sections = []
    for r in parallel.results:
        if r.ok:
            sections.append(f"### 模型：{r.model_name}\n{r.text}")
        else:
            sections.append(f"### 模型：{r.model_name}（失败）\n{r.error}")

    header = f"[AgentParliament]{len(parallel.successful)}/{len(parallel.results)}个模型成功完成。"
    if parallel.total_cost_usd > 0:
        header += f" 总成本：${parallel.total_cost_usd:.4f}"
    header += "\n以下是各模型的独立回答，请由主Agent自行对比共识与分歧。"

    # synthesize=False：现有行为不变，各模型回答原样拼接由主Agent自己综合
    if not synthesize:
        return _project_dir_warning(project_dir) + f"{header}\n\n" + "\n\n---\n\n".join(sections)

    # synthesize=True：额外跑一次合成模型，融合各模型回答输出单一结论。
    # 执行到此处时必有成功回答（all_failed 已在前面提前返回），合成失败不阻断，降级返回原始拼接结果。
    answers_block = "\n\n---\n\n".join(sections)
    synth_prompt = (
        "以下是多个模型对同一问题的独立回答。请综合它们的共识、裁决分歧，"
        "输出一个单一的高质量结论。不要简单平均，要批判性地择优："
        "若多数模型一致但某个模型有独到见解，请吸收独到部分；"
        "若模型间存在根本分歧，请明确指出并给出你的裁决与理由。\n\n"
        f"原始问题：{question}\n\n"
        f"{answers_block}\n\n"
        "请按以下结构输出：\n"
        "1.【综合结论】你对原始问题的直接回答\n"
        "2.【共识点】多个模型一致的结论\n"
        "3.【分歧与裁决】模型间分歧及你的裁决理由\n"
        "4.【采用理由】为何采用这一综合结论"
    )

    try:
        synth_result = run_with_chain(
            config=_get_config(),
            role=aggregator_role,
            prompt=_memory_block(cwd) + synth_prompt,
            cwd=cwd,
        )
    except ProfileError as exc:
        # 合成模型角色链配置错误时，降级返回原始拼接结果，并在开头标注
        return (
            _project_dir_warning(project_dir)
            + f"[AgentParliament]合成模型调用失败：{exc}\n\n"
            + f"{header}\n\n" + "\n\n---\n\n".join(sections)
        )

    if not synth_result.ok:
        # 合成模型整链失败时，降级返回原始拼接结果
        return (
            _project_dir_warning(project_dir)
            + f"{header}\n\n（合成模型失败，以下为各模型原始回答）\n\n"
            + "\n\n---\n\n".join(sections)
        )

    # 合成成功：先给单一综合结论，再附各模型原始回答，保留分歧信号便于主Agent追溯
    synth_header = (
        f"[AgentParliament]已由`{synth_result.model_used}`合成"
        f"{len(parallel.successful)}个模型的回答。"
    )
    if synth_result.degraded:
        synth_header += "\n注意：合成首选模型不可用，已降级。"
    total_cost = parallel.total_cost_usd + synth_result.total_cost_usd
    if total_cost > 0:
        synth_header += f" 本次总成本：${total_cost:.4f}"

    return (
        _project_dir_warning(project_dir)
        + synth_header + "\n\n" + synth_result.text
        + "\n\n———— 原始各模型回答 ————\n\n"
        + "\n\n---\n\n".join(sections)
    )

# 工具 5：validate_approach
@mcp.tool()
def validate_approach(
    approach: str,
    goal: str = "",
    role: str = "third_party",
    project_dir: str | None = None,
) -> str:
    """
    让另一个模型扮演反对者，对架构/方案/设计找漏洞
    适用场景：主Agent设计了一个方案后，在实施前想验证其可行性，避免踩坑。
    与independent_analysis的区别：本工具审查的是"尚未实施的方案"而非"已有的结论"

    Args:
        approach: 待验证的方案/架构/设计描述
        goal: 可选，方案要达成的目标，帮助副Agent判断方案是否偏离目标
        role: 使用哪条角色失败链，默认"third_party"
        project_dir: 副Agent的工作目录，必须传入当前项目根目录，以便副Agent读取该项目的CLAUDE.md共享记忆与docs/adr决策记录；不传则退回MCP所在目录，仅适合调试AgentParliament自身

    Returns:
        方案验证报告，含潜在风险、遗漏场景与改进建议
    """
    goal_block = ""
    if goal:
        goal_block = f"\n方案要达成的目标：{goal}\n请评估方案是否能达成此目标。"

    prompt = (
        "你是一名资深架构师和方案评审者，你的职责是扮演反对者，尽可能找出方案中的潜在问题。\n\n"
        "验证流程：\n"
        "1. 先用自己的话复述方案的核心思路，确认你准确理解了方案，而非攻击错误靶子\n"
        "2. 从可行性、边界条件、与现有系统的兼容性等维度逐项审视\n"
        "3. 对每个发现的风险，给出具体的触发场景而非泛泛而谈\n\n"
        "请按以下结构输出：\n"
        "1.【方案概要】用1-2句话概括方案的核心思路\n"
        "2.【潜在风险】按严重程度列出方案可能遇到的问题：\n"
        "   🔴 致命风险（会导致方案完全不可行）\n"
        "   🟠 重大风险（会导致方案部分失败或需要大幅调整）\n"
        "   🟡 一般风险（需要额外处理但可控）\n"
        "3.【遗漏场景】方案可能没有考虑到的边界情况或使用场景\n"
        "4.【改进建议】针对发现的风险，给出具体的改进方向\n"
        "5.【总体判断】方案是否值得推进（推荐/有条件推荐/不推荐），以及理由\n\n"
        "注意：你的目标是帮方案提前排雷，而非证明方案不可行。如果方案整体可行，请诚实说明。\n\n"
        f"待验证的方案：{approach}"
        f"{goal_block}"
        f"{_READONLY_NOTICE}"
    )

    try:
        cwd = _resolve_cwd(project_dir)
        result = run_with_chain(
            config=_get_config(),
            role=role,
            prompt=_memory_block(cwd) + prompt,
            cwd=cwd,
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    return _project_dir_warning(project_dir) + _format_result(result)

# 工具 6：test_audit
@mcp.tool()
def test_audit(
    source_files: list[str],
    test_files: list[str] | None = None,
    focus: str = "", role: str = "reviewer",
    project_dir: str | None = None,
) -> str:
    """
    委托一个只读副Agent审计测试覆盖情况，找出缺失的测试场景与薄弱的边界覆盖
    适用场景：实现完一个功能后，想知道"哪些情况还没测到"。
    本工具不运行测试，而是静态分析源码逻辑分支与现有测试，指出未覆盖的路径、边界条件与异常场景
    与peer_review的区别：peer_review审查代码改动本身的对错，test_audit专注于"测试是否够全"

    Args:
        source_files: 被测源码文件路径列表，副Agent会阅读它们以理解需要覆盖的逻辑分支
        test_files: 可选，现有测试文件路径列表。传入后副Agent会对比现有测试与源码，找出缺口；不传则只基于源码给出建议测试清单
        focus: 可选，审计重点，如"异常路径""并发""边界值"，留空则全面审计
        role: 使用哪条角色失败链，默认"reviewer"
        project_dir: 副Agent的工作目录，必须传入当前项目根目录，以便副Agent读取该项目的CLAUDE.md共享记忆与docs/adr决策记录；不传则退回MCP所在目录，仅适合调试AgentParliament自身

    Returns:
        测试审计报告，含已覆盖情况、缺失场景与建议补充的测试用例
    """
    source_block = "\n".join(f"- {p}" for p in source_files)
    if test_files:
        test_block = "\n".join(f"- {p}" for p in test_files)
        coverage_intro = (
            "请对比下方源码与现有测试，找出源码中尚未被测试覆盖的逻辑分支、边界条件与异常路径。\n\n"
            f"被测源码：\n{source_block}\n\n现有测试：\n{test_block}\n\n"
        )
        all_paths = list(source_files) + list(test_files)
    else:
        coverage_intro = (
            "下方为被测源码，目前没有提供现有测试文件。"
            "请基于源码逻辑给出一份应当覆盖的测试用例清单。\n\n"
            f"被测源码：\n{source_block}\n\n"
        )
        all_paths = list(source_files)

    # 放开所有涉及文件的父目录，确保副Agent的Read能访问
    extra_dirs = sorted({
        str(Path(p).expanduser().resolve().parent) for p in all_paths
    })

    focus_line = f"本次审计重点：{focus}\n\n" if focus else ""
    prompt = (
        "你是一名严谨的测试工程师。请阅读给定的文件，审计测试覆盖情况。\n\n"
        "审计流程：\n"
        "1. 先梳理源码的核心逻辑路径和关键分支，确保你理解了代码要做什么\n"
        "2. 对照现有测试（如有），逐条逻辑路径检查是否有测试覆盖\n"
        "3. 对未覆盖的路径，评估其风险等级并给出具体测试用例\n\n"
        f"{coverage_intro}"
        f"{focus_line}"
        "请按以下结构输出：\n"
        "1.【核心逻辑】源码的主要逻辑路径和关键分支（帮助确认你理解了代码）\n"
        "2.【已覆盖】现有测试已经覆盖了哪些场景（无现有测试时跳过此项）\n"
        "3.【缺失场景】按重要性列出尚未覆盖的逻辑分支、边界条件、异常路径：\n"
        "   🔴 关键缺口（核心逻辑或易错路径未测）\n"
        "   🟠 重要缺口（边界值、异常处理未测）\n"
        "   🟡 次要缺口（锦上添花的场景）\n"
        "4.【建议用例】针对缺失场景，给出具体的测试用例描述（输入、预期输出、验证点）\n"
        "5.【总体评价】当前测试充分度（充分/基本充分/不足），以及优先补充建议"
        f"{_READONLY_NOTICE}"
    )

    try:
        cwd = _resolve_cwd(project_dir)
        result = run_with_chain(
            config=_get_config(),
            role=role,
            prompt=_memory_block(cwd) + prompt,
            cwd=cwd,
            extra_dirs=extra_dirs,
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    return _project_dir_warning(project_dir) + _format_result(result)

# advisor_analysis 触发升级的哨兵标记。中等模型在草稿末尾输出此标记表示「我不确定，请求助强模型」
_NEED_ADVISOR_TAG = "[NEED_ADVISOR]"

# 工具 7：advisor_analysis
@mcp.tool()
def advisor_analysis(
    task: str,
    draft_role: str = "researcher",
    advisor_role: str = "advisor",
    force_advisor: bool = False,
    project_dir: str | None = None,
    context_files: list[str] | None = None,
) -> str:
    """
    两阶段战略求助：中等模型先出草稿，不确定或高风险时才升级求助强模型（如claude）。
    这是 FrugalGPT「按置信度升级」思路的变体——不是失败才升级，是不确定才升级，
    因此大多数调用只跑一次中等模型，成本与 delegate_research 相当。

    与 independent_analysis 的区别：independent_analysis 总是跑第三方模型做批判；
    本工具是条件触发（省成本），且专为「中等模型可能不够、但又不想每次都烧强模型」的场景设计。

    [使用前提] 调用前你已对问题有初步判断。本工具用于「想用便宜模型兜底、关键处再求助强模型」的场景。
    在 profiles.json 中把 advisor_role 配为含 claude 的角色链（如 ["claude","glm"]）才能发挥效果。

    Args:
        task: 要分析的问题/任务
        draft_role: 出草稿的中等模型角色链，默认"researcher"
        advisor_role: 求助的强模型角色链，默认"advisor"（建议在profiles.json配为["claude","glm"]）
        force_advisor: 主Agent强制升级求助。高风险决策（架构选型/安全/数据完整性）时设为True
        project_dir: 副Agent的工作目录，必须传入当前项目根目录，以便副Agent读取该项目的CLAUDE.md共享记忆与docs/adr决策记录；不传则退回MCP所在目录，仅适合调试AgentParliament自身
        context_files: 可选，相关文件路径列表，会拼进草稿阶段的提示词

    Returns:
        不触发升级时：仅返回中等模型草稿（成本同 delegate_research）。
        触发升级时：先返回草稿，再返回强模型顾问意见，让主Agent看到两阶段对比。
    """
    # 草稿阶段：要求中等模型在不确定时自评并输出升级标记。这是初版置信度信号，不完美但成本极低
    draft_prompt = (
        f"{task}\n\n"
        "请给出你的结论。如果你对答案有把握，直接给结论即可；"
        "如果你不确定、或问题涉及高风险决策（架构选型/安全/数据完整性等），"
        f"在回答最末尾另起一行写出标记：{_NEED_ADVISOR_TAG}"
    )
    if context_files:
        joined = "\n".join(f"- {p}" for p in context_files)
        draft_prompt += f"\n\n请优先阅读以下相关文件，无需扫描整个项目：\n{joined}"

    try:
        cwd = _resolve_cwd(project_dir)
        draft = run_with_chain(
            config=_get_config(),
            role=draft_role,
            prompt=_memory_block(cwd) + draft_prompt,
            cwd=cwd,
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    # 升级条件：主Agent强制 或 中等模型自评不确定（草稿末尾含升级标记）
    escalate = force_advisor or (
        draft.ok and _NEED_ADVISOR_TAG in draft.text
    )

    if not escalate:
        # 未升级：只返回草稿。force_advisor=False 且模型有把握时走此分支
        return _project_dir_warning(project_dir) + _format_result(draft)

    # 升级：求助强模型审查草稿并给出权威结论
    advisor_prompt = (
        f"原始任务：{task}\n\n"
        "中等模型的草稿结论如下（仅供参考，可能存在盲区或错误）：\n"
        f"{draft.text}\n\n"
        "你作为资深顾问，请审查草稿：若草稿正确请明确认可并补充论据；"
        "若有问题请指出并给出修正后的权威结论。"
    )

    try:
        advisor = run_with_chain(
            config=_get_config(),
            role=advisor_role,
            prompt=_memory_block(cwd) + advisor_prompt,
            cwd=cwd,
        )
    except ProfileError as exc:
        # 强模型角色链配置错误：返回草稿并标注求助失败
        return (
            _project_dir_warning(project_dir)
            + _format_result(draft)
            + f"\n\n[AgentParliament]强模型求助失败：{exc}"
        )

    # 返回两阶段：草稿在前（主Agent可对比），顾问意见在后（权威结论）
    return (
        _project_dir_warning(project_dir)
        + "【阶段1：中等模型草稿】\n" + _format_result(draft)
        + "\n\n【阶段2：强模型顾问意见】\n" + _format_result(advisor)
    )


def main() -> None:
    """ 以stdio方式启动MCP服务，供命令行入口（pyproject.toml的scripts）与直接运行共用 """
    mcp.run()

if __name__ == "__main__":
    main()
