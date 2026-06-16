"""
server.py——AgentParliament的MCP入口

通过stdio暴露5个只读工具给主Agent：
1. delegate_research   —— 调研：把问题与相关文件交给副模型调研，回传结论
2. peer_review         —— 代码审查：把git diff交给副模型审查，回传分级问题
3. independent_analysis—— 独立分析：让第三方模型批判性审视已有结论，找盲区与漏洞
4. consensus           —— 多模型共识：并行让多个模型回答同一问题，自动对比共识与分歧
5. validate_approach   —— 方案验证：让另一个模型扮演反对者，对架构/方案找漏洞

所有工具的副Agent都以只读plan模式运行，不会修改任何文件；改文件的动作始终留在主进程，由用户审阅后执行
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
            lines.append(f"  - {attempt.model_name}: {error_detail}")
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
        project_dir: 副Agent的工作目录，项目根目录。不传则用MCP所在目录
        context_files: 可选，主Agent已定位的相关文件路径列表。会拼进提示词，引导副Agent优先阅读这些文件，避免从头扫描整个项目造成浪费
        allow_web: 可选，是否允许副Agent联网搜索（WebSearch/WebFetch）。默认False（仅本地代码调研）。
            当调研问题涉及最新文档、版本特性、外部资料时设为True；副Agent仍处于只读plan模式，联网仅用于获取信息，不会改文件

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

    # 联网搜索默认关闭，仅在主Agent显式开启时放开WebSearch/WebFetch这两个只读联网工具
    allowed_tools = ["WebSearch", "WebFetch"] if allow_web else None

    try:
        result = run_with_chain(
            config=_get_config(),
            role=role,
            prompt=prompt,
            cwd=_resolve_cwd(project_dir),
            allowed_tools=allowed_tools,
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    return _format_result(result)

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
        project_dir: 副Agent的工作目录，便于它在需要时阅读改动涉及的上下文文件
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
        result = run_with_chain(
            config=_get_config(),
            role=role,
            prompt=prompt,
            cwd=_resolve_cwd(project_dir),
            extra_dirs=extra_dirs,
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    return _format_result(result)

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
        project_dir: 副Agent的工作目录，便于它在需要时查阅代码上下文

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
        result = run_with_chain(
            config=_get_config(),
            role=role,
            prompt=prompt,
            cwd=_resolve_cwd(project_dir),
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    return _format_result(result)

# 工具 4：consensus
@mcp.tool()
def consensus(
    question: str,
    role: str = "third_party",
    project_dir: str | None = None,
    context_files: list[str] | None = None,
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
        project_dir: 副Agent的工作目录，不传则用MCP所在目录
        context_files: 可选，主Agent已定位的相关文件路径列表

    Returns:
        多模型回答的对比报告，含各模型独立回答、共识点、分歧点与综合判断
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
        parallel = run_parallel(
            config=_get_config(),
            role=role,
            prompt=prompt,
            cwd=_resolve_cwd(project_dir),
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    if parallel.all_failed:
        return _format_parallel_result(parallel)

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

    return f"{header}\n\n" + "\n\n---\n\n".join(sections)

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
        project_dir: 副Agent的工作目录，便于它查阅项目现有代码以评估方案可行性

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
        result = run_with_chain(
            config=_get_config(),
            role=role,
            prompt=prompt,
            cwd=_resolve_cwd(project_dir),
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    return _format_result(result)

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
        project_dir: 副Agent的工作目录，便于它阅读传入的文件

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
        result = run_with_chain(
            config=_get_config(),
            role=role,
            prompt=prompt,
            cwd=_resolve_cwd(project_dir),
            extra_dirs=extra_dirs,
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    return _format_result(result)


def main() -> None:
    """ 以stdio方式启动MCP服务，供命令行入口（pyproject.toml的scripts）与直接运行共用 """
    mcp.run()

if __name__ == "__main__":
    main()
