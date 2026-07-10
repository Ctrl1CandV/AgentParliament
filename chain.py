"""
chain.py——AgentParliament的链调度核心与共享辅助

职责：
1. _call_tool_by_name：内部工具调度器，按原子工具名执行并返回结构化dict
   （供delegate_chain复用prompt纯函数与run_with_chain，保证单工具调用与链内调用产出一致）
2. _execute_delegate_chain：delegate_chain的编排逻辑
   （stage循环、占位符替换、自动填充、stage参数校验、synthesize综合、结果渲染）
3. _run_synthesize：synthesize综合步骤
4. 共享辅助：_get_config（延迟加载缓存）、_resolve_cwd、_extra_dirs_from_files、工具白/黑名单

本模块不import server（避免循环依赖），server.py与chain.py所需共享辅助均在本模块定义。
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

from runner import (
    Config,
    ProfileError,
    RunResult,
    load_config,
    run_with_chain,
    run_parallel,
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
    _chain_error,
    _chain_result_from_run,
    _project_dir_warning,
    _render_chain_result,
    _truncate_for_chain,
    _validate_structured_review,
)

# prompt改由stdin传入后已无命令行长度限制，此处仅保留一个宽松上限防止极端输入耗尽内存
_DIFF_SIZE_LIMIT = 200000

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

def _extra_dirs_from_files(context_files: list[str] | None) -> list[str] | None:
    """ 把文件路径列表转换为 --add-dir目录列表，供handler传给run_with_chain """
    if not context_files:
        return None
    return sorted({
        str(Path(p).expanduser().resolve().parent)
        for p in context_files
    })

# delegate_research使用的只读工具白名单，不再依赖 plan 模式
# default模式下用allowed_tools显式限定可调用工具，同时用disallowed_tools黑名单兜底，双重护栏
_RESEARCH_TOOLS_LOCAL = [
    "Read", "Grep", "Glob",
    "Bash(git log:*)", "Bash(git show:*)", "Bash(git blame:*)", "Bash(git diff:*)",
    "Bash(ls:*)", "Bash(dir:*)", "Bash(find:*)", "Bash(wc:*)", "Bash(cat:*)",
]
_RESEARCH_TOOLS_WEB = list(_RESEARCH_TOOLS_LOCAL) + ["WebSearch", "WebFetch"]

# 写操作工具黑名单：无论allowed_tools如何配置，这些工具一律拒绝
_DISALLOWED_TOOLS = ["Write", "Edit", "NotebookEdit", "Bash"]

# delegate_chain支持的原子工具集合
_ALLOWED_CHAIN_TOOLS = frozenset({
    "delegate_research", "peer_review", "independent_analysis",
    "consensus", "validate_approach", "test_audit", "advisor_analysis",
})

async def _call_tool_by_name(
    tool_name: str,
    *,
    project_dir: str,
    role: str = "third_party",
    context_files: list[str] | None = None,
    # delegate_research参数
    question: str = "",
    allow_web: bool = False,
    # advisor_analysis参数（公开工具签名用的字段名是task，与question/original_task并列作fallback）
    task: str = "",
    # peer_review参数
    diff: str = "",
    focus: str = "",
    structured: bool = False,
    # independent_analysis与validate_approach参数
    original_task: str = "",
    existing_result: str = "",
    approach: str = "",
    goal: str = "",
    concern: str = "",
    # consensus参数
    synthesize: bool = False,
    aggregator_role: str = "reviewer",
    # test_audit参数
    source_files: list[str] | None = None,
    test_files: list[str] | None = None,
    # advisor_analysis参数
    draft_role: str = "researcher",
    advisor_role: str = "advisor",
    force_advisor: bool = False,
    # 性能优化：delegate_chain 顶层读一次记忆块复用给所有步骤，避免每步重复读 CLAUDE.md/SPEC/ADR
    memory_override: str | None = None,
) -> dict:
    """
    内部工具调度器：根据原子工具名执行并返回结构化结果
    仅供delegate_chain内部复用prompt纯函数和run_with_chain
    返回dict: {ok, text, model_used, cost_usd, error?}
    """
    # 校验工具名
    if tool_name not in _ALLOWED_CHAIN_TOOLS:
        return _chain_error(f"delegate_chain 不支持的工具：{tool_name}")

    try:
        cwd = _resolve_cwd(project_dir)
        config = _get_config()
    except ProfileError as exc:
        return _chain_error(str(exc))

    # delegate_chain 复用顶层记忆块；单工具直接调用（memory_override=None）时现读
    memory = memory_override if memory_override is not None else _memory_block(cwd)
    extra_dirs = _extra_dirs_from_files(context_files)

    # 构建 prompt：复用纯函数保证一致性
    if tool_name == "delegate_research":
        prompt = _build_research_prompt(question, context_files)
        allowed = list(_RESEARCH_TOOLS_WEB if allow_web else _RESEARCH_TOOLS_LOCAL)
        try:
            result = await asyncio.to_thread(
                run_with_chain,
                config=config, role=role, prompt=memory + prompt,
                cwd=cwd, allowed_tools=allowed,
                permission_mode="default", disallowed_tools=_DISALLOWED_TOOLS,
            )
        except ProfileError as exc:
            return _chain_error(str(exc))
        return _chain_result_from_run(result)

    if tool_name == "peer_review":
        if len(diff) > _DIFF_SIZE_LIMIT:
            return _chain_error(
                f"peer_review diff 为 {len(diff)} 字符，超过安全上限 {_DIFF_SIZE_LIMIT}，"
                f"请改用 context_files 传文件路径或只传关键改动 diff"
            )
        prefix, focus_line, instruction = _build_review_prompt(diff, focus, context_files, structured)
        prompt = f"{prefix}{instruction}{focus_line}待审查的改动（diff）：\n```diff\n{diff}\n```{_READONLY_NOTICE}"
        try:
            result = await asyncio.to_thread(
                run_with_chain,
                config=config, role=role, prompt=memory + prompt,
                cwd=cwd, extra_dirs=extra_dirs,
            )
        except ProfileError as exc:
            return _chain_error(str(exc))
        text = result.text
        if structured and result.ok:
            ok, cleaned, msg = _validate_structured_review(text)
            text = cleaned if ok else f"{msg}\n\n原始输出：\n{text}"
        return _chain_result_from_run(result, text_override=text)

    if tool_name == "independent_analysis":
        prompt = _build_independent_analysis_prompt(existing_result, concern, context_files, original_task)
        try:
            result = await asyncio.to_thread(
                run_with_chain,
                config=config, role=role, prompt=memory + prompt,
                cwd=cwd, extra_dirs=extra_dirs,
            )
        except ProfileError as exc:
            return _chain_error(str(exc))
        return _chain_result_from_run(result)

    if tool_name == "validate_approach":
        prompt = _build_validate_approach_prompt(approach, goal, context_files)
        try:
            result = await asyncio.to_thread(
                run_with_chain,
                config=config, role=role, prompt=memory + prompt,
                cwd=cwd, extra_dirs=extra_dirs,
            )
        except ProfileError as exc:
            return _chain_error(str(exc))
        return _chain_result_from_run(result)

    if tool_name == "consensus":
        prompt = _build_consensus_prompt(question, context_files)
        try:
            parallel = await asyncio.to_thread(
                run_parallel,
                config=config, role=role, prompt=memory + prompt,
                cwd=cwd, extra_dirs=extra_dirs,
            )
        except ProfileError as exc:
            return _chain_error(str(exc))
        if parallel.all_failed:
            return _chain_error("所有模型均失败", cost_usd=parallel.total_cost_usd)
        # synthesize 在 delegate_chain 内部不作为独立步骤处理，这里按 synthesize=False 返回拼接
        sections = []
        for r in parallel.results:
            if r.ok:
                sections.append(f"### 模型：{r.model_name}\n{r.text}")
            else:
                sections.append(f"### 模型：{r.model_name}（失败）\n{r.error}")
        header = f"[AgentParliament]{len(parallel.successful)}/{len(parallel.results)}个模型成功完成。"
        if parallel.total_cost_usd > 0:
            header += f" 总成本：${parallel.total_cost_usd:.4f}"
        header += "\n以下是各模型的独立回答，请由链的下一步或综合步骤对比共识与分歧。"
        return {
            "ok": True, "text": f"{header}\n\n" + "\n\n---\n\n".join(sections),
            "model_used": "parallel", "cost_usd": parallel.total_cost_usd,
        }

    if tool_name == "test_audit":
        prompt = _build_test_audit_full_prompt(source_files or [], test_files, focus)
        all_paths = list(source_files or []) + list(test_files or [])
        audit_dirs = _extra_dirs_from_files(all_paths)
        try:
            result = await asyncio.to_thread(
                run_with_chain,
                config=config, role=role, prompt=memory + prompt,
                cwd=cwd, extra_dirs=audit_dirs,
            )
        except ProfileError as exc:
            return _chain_error(str(exc))
        return _chain_result_from_run(result)

    if tool_name == "advisor_analysis":
        # task是advisor_analysis对外公开签名的主字段名，question/original_task是delegate_chain内部兼容的别名
        advisor_task = task or question or original_task
        prompt = _build_advisor_draft_prompt(advisor_task, context_files)
        try:
            draft = await asyncio.to_thread(
                run_with_chain,
                config=config, role=draft_role, prompt=memory + prompt,
                cwd=cwd, extra_dirs=extra_dirs,
            )
        except ProfileError as exc:
            return _chain_error(str(exc))
        escalate = force_advisor or (draft.ok and _NEED_ADVISOR_TAG in draft.text)
        if not escalate:
            return _chain_result_from_run(draft)
        # 升级：强模型顾问审查草稿
        advisor_prompt = (
            f"原始任务：{advisor_task}\n\n"
            "中等模型的草稿结论如下（仅供参考，可能存在盲区或错误）：\n"
            f"{draft.text}\n\n"
            "你作为资深顾问，请审查草稿：若草稿正确请明确认可并补充论据；"
            "若有问题请指出并给出修正后的权威结论。"
        )
        try:
            advisor = await asyncio.to_thread(
                run_with_chain,
                config=config, role=advisor_role, prompt=memory + advisor_prompt,
                cwd=cwd, extra_dirs=extra_dirs,
            )
        except ProfileError as exc:
            # 强模型升级失败：返回草稿并标注求助失败（ok=True，因为有草稿可用）
            return {
                "ok": True, "text": draft.text,
                "model_used": draft.model_used, "cost_usd": draft.total_cost_usd,
                "error": f"强模型升级失败，返回草稿：{exc}",
            }
        if not advisor.ok:
            return {
                "ok": True, "text": draft.text,
                "model_used": draft.model_used, "cost_usd": draft.total_cost_usd,
                "error": "顾问模型失败，返回草稿",
            }
        return {
            "ok": True,
            "text": "【阶段1：中等模型草稿】\n" + draft.text + "\n\n【阶段2：强模型顾问意见】\n" + advisor.text,
            "model_used": advisor.model_used, "cost_usd": draft.total_cost_usd + advisor.total_cost_usd,
        }

    # 不可达防护（_ALLOWED_CHAIN_TOOLS 校验已确保不会到达这里）
    return _chain_error(f"未知工具：{tool_name}")

# delegate_chain 校验 stage 参数用的合法参数集合，与 _call_tool_by_name 签名自动同步
_CALL_TOOL_KWARGS = frozenset(inspect.signature(_call_tool_by_name).parameters) - {"tool_name"}

async def _run_synthesize(
    task: str,
    results: list[dict],
    project_dir: str,
    aggregator_role: str,
) -> dict:
    """ synthesize综合步骤：融合各步骤结果输出单一结论 """
    answers_block = "\n\n---\n\n".join(
        f"【步骤 {i+1}】{r.get('model_used', '?')}\n{r.get('text', '')}"
        for i, r in enumerate(results)
        if r.get("ok") and r.get("text")
    )
    synth_prompt = (
        "以下是多个步骤对同一任务的分析结果。请综合它们的共识、裁决分歧，"
        "输出一个单一的高质量结论。不要简单平均，要批判性地择优。\n\n"
        f"原始任务：{task}\n\n"
        f"各步骤结果：\n{answers_block}\n\n"
        "请按以下结构输出：\n"
        "1.【综合结论】对原始任务的直接回答\n"
        "2.【关键共识】多个步骤一致的结论\n"
        "3.【分歧与裁决】步骤间的分歧及你的裁决\n"
        "4.【采用理由】为何采用这一综合结论"
    )
    try:
        cwd = _resolve_cwd(project_dir)
        result = await asyncio.to_thread(
            run_with_chain,
            config=_get_config(),
            role=aggregator_role,
            prompt=_memory_block(cwd) + synth_prompt,
            cwd=cwd,
            # cwd 已是 project_dir，子 Agent 本就能读取项目文件，无需额外 --add-dir
            extra_dirs=None,
        )
    except ProfileError as exc:
        return _chain_error(str(exc))

    if not result.ok:
        return _chain_error("综合模型失败", cost_usd=result.total_cost_usd)
    return {
        "ok": True, "text": result.text, "model_used": result.model_used, "cost_usd": result.total_cost_usd,
    }

async def _execute_delegate_chain(
    task: str,
    stages: list[dict],
    project_dir: str,
    synthesize: bool = False,
    aggregator_role: str = "reviewer",
) -> str:
    """
    delegate_chain 的编排逻辑：主Agent定义思维链结构，本函数依次执行
    全部成功时含最终结论 + 各步骤详情；单步失败时停链，返回已完成步骤 + 错误
    最终结论无论是否synthesize都为最后一步的输出（synthesize=True 时为综合步骤输出）
    """
    if not stages:
        return "[AgentParliament]delegate_chain 调用错误：stages 为空，至少需要 1 个步骤。"

    project_dir_warning = _project_dir_warning(project_dir)

    # 顶层读一次记忆块，供所有步骤复用，避免 N 步重复读 CLAUDE.md/SPEC.md/docs/adr
    try:
        chain_cwd = _resolve_cwd(project_dir)
    except ProfileError as exc:
        return f"[AgentParliament] 配置或调用错误：{exc}"
    chain_memory = _memory_block(chain_cwd)

    results: list[dict] = []
    total_cost = 0.0

    for i, stage in enumerate(stages):
        tool_name = stage.get("tool", "")
        custom_prompt = stage.get("prompt", "") or ""
        # role单独取出，避免下方**tool_kwargs展开时与显式传入的role=形参冲突（TypeError: multiple values）
        stage_role = stage.get("role") or "third_party"

        # 提取工具特有参数，直接传给_call_tool_by_name
        # memory_override 是内部参数（chain 顶层复用记忆块），不由 stage 指定，排除以免与下方显式传参冲突
        tool_kwargs = {k: v for k, v in stage.items() if k not in ("tool", "prompt", "role", "project_dir", "memory_override")}

        # 校验stage参数：未知key会让_call_tool_by_name抛TypeError，提前给出可读错误
        unknown_kwargs = set(tool_kwargs) - _CALL_TOOL_KWARGS
        if unknown_kwargs:
            return (
                f"[AgentParliament]delegate_chain 步骤 {i+1}（{tool_name}）"
                f"含未知参数：{sorted(unknown_kwargs)}。"
                f"合法参数为各原子工具的入参，详见 _call_tool_by_name 签名。"
            )

        # 确保必填的工具参数有默认值
        if tool_name == "peer_review" and "diff" not in tool_kwargs:
            return f"[AgentParliament]delegate_chain 步骤 {i+1}（peer_review）缺少 diff 参数。"
        if tool_name == "test_audit" and "source_files" not in tool_kwargs:
            return f"[AgentParliament]delegate_chain 步骤 {i+1}（test_audit）缺少 source_files 参数。"

        # __PREVIOUS_RESULT__和__TASK__占位符替换
        previous_text = results[-1]["text"] if results else ""
        truncated_previous = _truncate_for_chain(previous_text)
        placeholders = {"__TASK__": task, "__PREVIOUS_RESULT__": truncated_previous}

        def _replace_placeholders(text: str) -> str:
            for k, v in placeholders.items():
                text = text.replace(k, v)
            return text

        enriched = _replace_placeholders(custom_prompt) if custom_prompt else ""

        # 自动填充：如果未提供主字段，用 task + enriched 填充
        if tool_name in ("delegate_research", "consensus"):
            if "question" not in tool_kwargs:
                tool_kwargs["question"] = f"{task}\n\n{enriched}" if enriched else task
            elif enriched:
                tool_kwargs["question"] = f"{tool_kwargs['question']}\n\n{enriched}"
        elif tool_name == "independent_analysis":
            if "original_task" not in tool_kwargs:
                tool_kwargs["original_task"] = task
            if "existing_result" not in tool_kwargs and previous_text:
                tool_kwargs["existing_result"] = truncated_previous
            if enriched:
                tool_kwargs["concern"] = enriched
        elif tool_name == "validate_approach":
            if "approach" not in tool_kwargs:
                tool_kwargs["approach"] = f"{task}\n\n{enriched}" if enriched else task
            elif enriched:
                tool_kwargs["approach"] = f"{tool_kwargs['approach']}\n\n{enriched}"
        elif tool_name == "peer_review":
            if enriched:
                tool_kwargs["focus"] = enriched
        elif tool_name == "test_audit":
            if enriched:
                tool_kwargs["focus"] = enriched
        elif tool_name == "advisor_analysis":
            if "question" not in tool_kwargs:
                tool_kwargs["question"] = f"{task}\n\n{enriched}" if enriched else task
            elif enriched:
                tool_kwargs["question"] = f"{tool_kwargs['question']}\n\n{enriched}"

        step_result = await _call_tool_by_name(
            tool_name, project_dir=project_dir, role=stage_role,
            memory_override=chain_memory, **tool_kwargs,
        )
        results.append(step_result)
        total_cost += step_result.get("cost_usd", 0.0)

        if not step_result["ok"]:
            return _render_chain_result(task, results, total_cost, failed_at=i, project_dir_warning=project_dir_warning)

    # synthesize 综合步骤
    if synthesize:
        synth_result = await _run_synthesize(task, results, project_dir, aggregator_role)
        results.append(synth_result)
        total_cost += synth_result.get("cost_usd", 0.0)
        if not synth_result["ok"]:
            return _render_chain_result(task, results, total_cost, failed_at=len(stages), project_dir_warning=project_dir_warning)

    return _render_chain_result(task, results, total_cost, project_dir_warning=project_dir_warning)
