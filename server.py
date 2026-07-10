"""
server.py——AgentParliament的MCP入口

通过stdio暴露9个工具给主Agent：
1. delegate_research   —— 调研：把问题与相关文件交给副模型调研，回传结论
2. peer_review         —— 代码审查：把git diff交给副模型审查，回传分级问题
3. independent_analysis—— 独立分析：让第三方模型批判性审视已有结论，找盲区与漏洞
4. consensus           —— 多模型共识：并行让多个模型回答同一问题，自动对比共识与分歧；可开启synthesize让合成模型融合出单一结论
5. validate_approach   —— 方案验证：让另一个模型扮演反对者，对架构/方案找漏洞
6. test_audit          —— 测试审计：静态分析源码与测试，找出未覆盖的逻辑分支与边界
7. advisor_analysis    —— 战略求助：中等模型先出草稿，不确定或高风险时条件升级求助强模型
8. delegate_chain      —— 多步推理链：主Agent定义多步思维链结构，server.py依次执行；链内子Agent可全文件读取
9. delegate_dialogue   —— 带对话能力的调研：子Agent可通过[ASK_PARENT]标记向主Agent提问，主Agent带session_id+answer续答

所有工具的副Agent都以只读模式运行，不会修改任何文件；改文件的动作始终留在主进程，由用户审阅后执行
- peer_review / independent_analysis / consensus / validate_approach / test_audit / advisor_analysis：
  使用 --permission-mode plan（Claude Code内置禁止所有写操作）
- delegate_research：使用 --permission-mode default + 显式只读工具白名单 + 写操作黑名单
  （plan模式的"提案后等待人类审批"语义会让headless单轮调研卡在"等待审阅"，故改用default模式）

共享记忆：每个工具调用前会显式读取副Agent工作目录下的CLAUDE.md（项目共享记忆体），
注入到prompt开头，使副Agent确定性地获得项目的领域语言、已定架构决策与硬约束，
而不依赖Claude Code在headless模式下是否自动加载CLAUDE.md。记忆体缺失或读取失败时静默降级，不影响工具主流程。
未传project_dir时在返回结果开头标注警告，提示记忆体未注入，避免主Agent在不知情下使用缺少项目上下文的结论。

本文件只保留MCP工具定义（接口层）；prompt构造、记忆注入、结果渲染、链调度分别拆到
prompts.py / render.py / chain.py，runner.py 为执行层。
"""
from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from runner import (
    ProfileError,
    run_parallel,
    run_with_chain,
)
from prompts import (
    _build_advisor_draft_prompt,
    _build_consensus_prompt,
    _build_independent_analysis_prompt,
    _build_research_prompt,
    _build_review_prompt,
    _build_test_audit_full_prompt,
    _build_validate_approach_prompt,
    _memory_block,
    _NEED_ADVISOR_TAG,
    _READONLY_NOTICE,
)
from render import (
    _format_parallel_result,
    _format_result,
    _project_dir_warning,
    _validate_structured_review,
)
from chain import (
    _DISALLOWED_TOOLS,
    _DIFF_SIZE_LIMIT,
    _RESEARCH_TOOLS_LOCAL,
    _RESEARCH_TOOLS_WEB,
    _execute_delegate_chain,
    _execute_delegate_dialogue,
    _extra_dirs_from_files,
    _get_config,
    _resolve_cwd,
)

mcp = FastMCP("AgentParliament")

# 工具 1：delegate_research
@mcp.tool()
async def delegate_research(
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
    cwd = _resolve_cwd(project_dir)
    prompt = _memory_block(cwd) + _build_research_prompt(question, context_files)
    allowed_tools = list(_RESEARCH_TOOLS_WEB if allow_web else _RESEARCH_TOOLS_LOCAL)

    try:
        result = await asyncio.to_thread(
            run_with_chain,
            config=_get_config(),
            role=role,
            prompt=prompt,
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
async def peer_review(
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

    # 默认约束副Agent只看diff不读文件；一旦传入context_files，则改为引导它阅读这些上下文文件，并把文件所在目录通过--add-dir放开，确保Read能访问cwd之外的路径
    extra_dirs = _extra_dirs_from_files(context_files)
    prefix, focus_line, instruction = _build_review_prompt(diff, focus, context_files, structured)

    prompt = (
        f"{prefix}"
        f"{instruction}"
        f"{focus_line}"
        f"待审查的改动（diff）：\n```diff\n{diff}\n```"
        f"{_READONLY_NOTICE}"
    )

    try:
        cwd = _resolve_cwd(project_dir)
        result = await asyncio.to_thread(
            run_with_chain,
            config=_get_config(),
            role=role,
            prompt=_memory_block(cwd) + prompt,
            cwd=cwd,
            extra_dirs=extra_dirs,
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    # structured=True时对模型输出做尽力解析：剥markdown fence、json.loads、校验必填字段
    # 解析失败不重试，但明确标注失败并保留原始输出供主Agent salvage
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
async def independent_analysis(
    original_task: str,
    existing_result: str,
    concern: str = "",
    role: str = "third_party",
    project_dir: str | None = None,
    context_files: list[str] | None = None,
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
        context_files: 可选，与本次分析相关的文件路径列表。传入后副Agent会阅读这些文件以理解原始结论所涉及的代码实现，避免纯文本审视的盲区

    Returns:
        结构化的独立分析报告，含共识、分歧与替代判断。
    """
    prompt = _build_independent_analysis_prompt(existing_result, concern, context_files, original_task)
    extra_dirs = _extra_dirs_from_files(context_files)

    try:
        cwd = _resolve_cwd(project_dir)
        result = await asyncio.to_thread(
            run_with_chain,
            config=_get_config(),
            role=role,
            prompt=_memory_block(cwd) + prompt,
            cwd=cwd,
            extra_dirs=extra_dirs,
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    return _project_dir_warning(project_dir) + _format_result(result)

# 工具 4：consensus
@mcp.tool()
async def consensus(
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
        aggregator_role: 可选，合成用的角色失败链，默认"reviewer"（选用始终存在的角色以保证合成不因角色缺失而失败）。
            若 profiles.json 配置了 "aggregator"(中等模型) 或 "strong_aggregator"(含claude等强模型) 角色，建议按场景改用，合成质量更贴合"融合多模型"的语义

    Returns:
        多模型回答的对比报告。
        synthesize=False：含各模型独立回答，由主Agent自行对比共识与分歧。
        synthesize=True：先给单一合成结论，再附各模型原始回答（保留分歧信号，便于主Agent追溯）。
    """
    prompt = _build_consensus_prompt(question, context_files)
    extra_dirs = _extra_dirs_from_files(context_files)

    try:
        cwd = _resolve_cwd(project_dir)
        parallel = await asyncio.to_thread(
            run_parallel,
            config=_get_config(),
            role=role,
            prompt=_memory_block(cwd) + prompt,
            cwd=cwd,
            extra_dirs=extra_dirs,
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

    # synthesize=True：额外跑一次合成模型，融合各模型回答输出单一结论
    # 执行到此处时必有成功回答（all_failed 已在前面提前返回），合成失败不阻断，降级返回原始拼接结果
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
        synth_result = await asyncio.to_thread(
            run_with_chain,
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
async def validate_approach(
    approach: str,
    goal: str = "",
    role: str = "third_party",
    project_dir: str | None = None,
    context_files: list[str] | None = None,
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
        context_files: 可选，与本次验证相关的文件路径列表。传入后副Agent会阅读这些文件以理解现有实现与约束

    Returns:
        方案验证报告，含潜在风险、遗漏场景与改进建议
    """
    prompt = _build_validate_approach_prompt(approach, goal, context_files)
    extra_dirs = _extra_dirs_from_files(context_files)

    try:
        cwd = _resolve_cwd(project_dir)
        result = await asyncio.to_thread(
            run_with_chain,
            config=_get_config(),
            role=role,
            prompt=_memory_block(cwd) + prompt,
            cwd=cwd,
            extra_dirs=extra_dirs,
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    return _project_dir_warning(project_dir) + _format_result(result)

# 工具 6：test_audit
@mcp.tool()
async def test_audit(
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
    all_paths = list(source_files) + list(test_files or [])
    # 放开所有涉及文件的父目录，确保副Agent的Read能访问
    extra_dirs = _extra_dirs_from_files(all_paths)

    prompt = _build_test_audit_full_prompt(source_files, test_files, focus)

    try:
        cwd = _resolve_cwd(project_dir)
        result = await asyncio.to_thread(
            run_with_chain,
            config=_get_config(),
            role=role,
            prompt=_memory_block(cwd) + prompt,
            cwd=cwd,
            extra_dirs=extra_dirs,
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    return _project_dir_warning(project_dir) + _format_result(result)

# 工具 7：advisor_analysis
@mcp.tool()
async def advisor_analysis(
    task: str,
    draft_role: str = "researcher",
    advisor_role: str = "advisor",
    force_advisor: bool = False,
    project_dir: str | None = None,
    context_files: list[str] | None = None,
) -> str:
    """
    两阶段战略求助：中等模型先出草稿，不确定或高风险时才升级求助强模型，这是FrugalGPT「按置信度升级」思路的变体——不是失败才升级，是不确定才升级，
    因此大多数调用只跑一次中等模型，成本与delegate_research相当

    与independent_analysis的区别：independent_analysis 总是跑第三方模型做批判；
    本工具是条件触发，且专为「中等模型可能不够、但又不想每次都烧强模型」的场景设计

    [使用前提] 调用前你已对问题有初步判断，本工具用于「想用便宜模型兜底、关键处再求助强模型」的场景

    Args:
        task: 要分析的问题/任务
        draft_role: 出草稿的中等模型角色链，默认"researcher"
        advisor_role: 求助的强模型角色链，默认"advisor"
        force_advisor: 主Agent强制升级求助。高风险决策（架构选型/安全/数据完整性）时设为True
        project_dir: 副Agent的工作目录，必须传入当前项目根目录，以便副Agent读取该项目的CLAUDE.md共享记忆与docs/adr决策记录
        context_files: 可选，相关文件路径列表，会拼进草稿阶段的提示词

    Returns:
        不触发升级时：仅返回中等模型草稿（成本同 delegate_research）。
        触发升级时：先返回草稿，再返回强模型顾问意见，让主Agent看到两阶段对比。
    """
    # 草稿阶段：要求中等模型在不确定时自评并输出升级标记，这是初版置信度信号，不完美但成本极低
    draft_prompt = _build_advisor_draft_prompt(task, context_files)
    extra_dirs = _extra_dirs_from_files(context_files)

    try:
        cwd = _resolve_cwd(project_dir)
        draft = await asyncio.to_thread(
            run_with_chain,
            config=_get_config(),
            role=draft_role,
            prompt=_memory_block(cwd) + draft_prompt,
            cwd=cwd,
            extra_dirs=extra_dirs,
        )
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"

    # 升级条件：主Agent强制或中等模型自评不确定
    escalate = force_advisor or (
        draft.ok and _NEED_ADVISOR_TAG in draft.text
    )

    if not escalate:
        # 未升级：只返回草稿，force_advisor=False且模型有把握时走此分支
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
        advisor = await asyncio.to_thread(
            run_with_chain,
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

# 工具 8：delegate_chain
@mcp.tool()
async def delegate_chain(
    task: str,
    stages: list[dict],
    project_dir: str,
    synthesize: bool = False,
    aggregator_role: str = "reviewer",
) -> str:
    """
    多步推理链：主Agent定义思维链结构，server.py依次执行
    适用于重要决策（架构选型、方案验证、复杂问题分析）需要完整链路深入思考的场景

    与现有7个工具的区别：
    - 现有工具每次只做一件事，子Agent只能读主Agent传入的context_files
    - delegate_chain把多个工具串成一条思考链，链内子Agent可自主阅读项目内全部文件，视野不受主Agent传入范围限制
    - 主Agent定义链路结构（第一步到最后一步分别是什么），prompt可由主Agent自定义或复用默认模板

    [使用前提] 调用前你已对问题形成初步判断。本工具用完整链路交叉验证你的判断，而非替你做判断
    delegate_chain成本 = 各步骤调用之和，仅在"非常重要且关键"的决策时使用

    Args:
        task: 整体任务描述，贯穿整个链路
        stages: 每步的定义列表，每项结构：{"tool": "工具名", "prompt": "可选自定义prompt（为空用默认模板）"},
                tool的可选值：delegate_research / peer_review / independent_analysis / consensus / validate_approach / test_audit / advisor_analysis,
                prompt支持占位符__TASK__（整体任务）和 __PREVIOUS_RESULT__（上一步完整输出，第一步忽略）
        project_dir: 项目根目录，所有步骤放开此目录的文件读取权限
        synthesize: 是否在最后加一步综合（用 aggregator_role 指定的角色融合各步结论），默认 False
        aggregator_role: 综合步骤使用的角色链。默认"reviewer"；重要决策可设 "strong_aggregator"

    Returns:
        完整链路结果。全部成功时含最终结论 + 各步骤详情；单步失败时停链，返回已完成步骤 + 错误
        最终结论无论是否synthesize都为最后一步的输出（synthesize=True 时为综合步骤输出）
    """
    return await _execute_delegate_chain(task, stages, project_dir, synthesize, aggregator_role)

# 工具 9：delegate_dialogue
@mcp.tool()
async def delegate_dialogue(
    question: str = "",
    project_dir: str = "",
    role: str = "researcher",
    context_files: list[str] | None = None,
    allow_web: bool = False,
    session_id: str = "",
    answer: str = "",
    max_dialogue: int = 3,
) -> str:
    """
    带对话能力的调研：子Agent在调研中途遇到需主Agent确认的方向时，可输出 [ASK_PARENT] 提问结束本轮；
    主Agent带 session_id+answer 再次调用，子Agent继续同一任务，形成"提问→回答→继续"闭环。

    与 delegate_research 的区别：delegate_research 是单轮 fire-and-forget，子Agent不能中途提问；
    本工具支持多轮接力对话，适合调研中存在方向性分歧、需主Agent拍板的场景。
    日常调研仍用 delegate_research，仅当预期调研中可能需要确认方向时用本工具。

    [使用前提] 调用前你已对问题形成初步判断。本工具用子Agent的独立视角交叉验证，而非替你做判断。
    子Agent的提问由你回答——你是决策方，不要把提问当作免思考的现成答案。

    Args:
        question: 要副Agent回答的调研问题，首次调用必填，续答时忽略
        project_dir: 副Agent工作目录，首次调用必填（对话需读取项目CLAUDE.md记忆体）；续答时从 session 取
        role: 使用哪条角色失败链，默认"researcher"
        context_files: 可选，主Agent已定位的相关文件路径列表，引导副Agent优先阅读
        allow_web: 可选，是否允许副Agent联网搜索，默认False
        session_id: 续答时传——首次调用返回的 session_id，用于接续对话上下文
        answer: 续答时必填——你对上一轮子Agent提问的回答
        max_dialogue: 最大对话轮数（含首轮），默认3，达上限后取子Agent当前结论结束，防无限循环

    Returns:
        首次/续答·子Agent提问时：含待确认问题+子Agent当前结论+session_id，提示你带 answer 续答
        对话结束时：子Agent最终结论+对话历史
        失败时：降级/失败尝试记录
    """
    return await _execute_delegate_dialogue(
        question, project_dir, role, context_files, allow_web,
        session_id, answer, max_dialogue,
    )

def main() -> None:
    """ 以stdio方式启动MCP服务，供命令行入口（pyproject.toml的scripts）与直接运行共用 """
    mcp.run()

if __name__ == "__main__":
    main()
