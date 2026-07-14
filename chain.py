"""
chain.py——AgentParliament的链调度核心、delegate_dialogue session 管理与共享辅助

职责：
1. _call_tool_by_name：内部工具调度器，按原子工具名执行并返回结构化dict
   （供delegate_chain复用prompt纯函数与run_with_chain，保证单工具调用与链内调用产出一致）
2. _execute_delegate_chain：delegate_chain的编排逻辑
   （stage循环、占位符替换、自动填充、stage参数校验、synthesize综合、结果渲染、动态超时）
3. _run_synthesize：synthesize综合步骤
4. _execute_delegate_dialogue：delegate_dialogue的编排逻辑
   （新建/续答分流、子Agent一轮、提问检测/续答接力、session管理）
5. 共享辅助：_get_config（延迟加载缓存）、_resolve_cwd、_extra_dirs_from_files、工具白/黑名单

本模块不import server（避免循环依赖），server.py与chain.py所需共享辅助均在本模块定义。
"""
from __future__ import annotations

import asyncio
import inspect
import threading
import time
import sys
import uuid
from pathlib import Path

from runner import (
    Config,
    ProfileError,
    RunResult,
    load_config,
    run_with_chain,
    run_with_chain_api,
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
    _build_verify_implementation_prompt,
    _memory_block,
    _NEED_ADVISOR_TAG,
    _READONLY_NOTICE,
    _build_dialogue_continue_prompt,
    _build_dialogue_prompt,
    detect_ask_parent,
)
from render import (
    _chain_error,
    _chain_result_from_run,
    _fmt_tokens,
    _project_dir_warning,
    _render_chain_result,
    _truncate_for_chain,
    _validate_structured_review,
    _format_ask_response,
    _format_dialogue_final,
    _format_result,
)
from worktree_manager import WorktreeSession, WorktreeError

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

# verify_implementation使用的 Tier 2 工具集：在 worktree 隔离副本内可写可执行
# 开放 Write/Edit（写测试文件）+ 常见测试/构建命令的白名单子命令
# 仍禁止危险操作：rm/push/网络写入等，通过 _VERIFY_DISALLOWED_TOOLS 黑名单兜底
_VERIFY_TOOLS = [
    "Read", "Grep", "Glob",
    "Write", "Edit",
    "Bash(ls:*)", "Bash(dir:*)", "Bash(find:*)", "Bash(wc:*)", "Bash(cat:*)", "Bash(tree:*)",
    "Bash(python:*)", "Bash(python3:*)", "Bash(py:*)", "Bash(pytest:*)", "Bash(pytest8:*)",
    "Bash(pip:*)", "Bash(uv:*)", "Bash(node:*)", "Bash(npm:*)", "Bash(npx:*)",
    "Bash(go:*)", "Bash(cargo:*)", "Bash(make:*)", "Bash(cmake:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
]
# Tier 2 的黑名单：禁止破坏性/外联操作。注意不含裸 Bash/Write/Edit（这些在白名单里按子命令放行）
_VERIFY_DISALLOWED_TOOLS = [
    "Bash(rm:*)", "Bash(rmdir:*)", "Bash(del:*)",
    "Bash(git push:*)", "Bash(git reset --hard:*)", "Bash(git clean:*)",
    "Bash(curl:*)", "Bash(wget:*)", "Bash(ssh:*)", "Bash(scp:*)",
]

# delegate_chain动态超时计算的膨胀系数增量。
# 越靠后的步骤要处理越多累积信息（__PREVIOUS_RESULT__），实际耗时越长。
# 第 i 步（从0开始）的膨胀系数 = 1.0 + i * INFLATION_FACTOR。
# 例如3步链路：1.0 + 1.15 + 1.3 = 3.45倍基础超时（对比简单求和的3.0倍）
# 推导依据：__PREVIOUS_RESULT__ 在 step i 时约为 step 0 的 (i+1) 倍长度，子 Agent 处理耗时
# 线性增长，实测 5 步链路每步增加约 10-20%，取中值 15%
INFLATION_FACTOR = 0.15

# 写操作工具黑名单：无论allowed_tools如何配置，这些工具一律拒绝
_DISALLOWED_TOOLS = ["Write", "Edit", "NotebookEdit", "Bash"]

# delegate_chain支持的原子工具集合
_ALLOWED_CHAIN_TOOLS = frozenset({
    "delegate_research", "peer_review", "independent_analysis",
    "consensus", "validate_approach", "test_audit", "advisor_analysis",
    "verify_implementation",
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
                allowed_tools=list(_RESEARCH_TOOLS_LOCAL),
                permission_mode="default",
                disallowed_tools=_DISALLOWED_TOOLS,
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
                allowed_tools=list(_RESEARCH_TOOLS_LOCAL),
                permission_mode="default",
                disallowed_tools=_DISALLOWED_TOOLS,
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
            return _chain_error(
                "所有模型均失败", cost_usd=parallel.total_cost_usd,
                input_tokens=parallel.total_input_tokens, output_tokens=parallel.total_output_tokens,
            )
        # synthesize 在 delegate_chain 内部不作为独立步骤处理，这里按 synthesize=False 返回拼接
        sections = []
        for r in parallel.results:
            if r.ok:
                sections.append(f"### 模型：{r.model_name}\n{r.text}")
            else:
                sections.append(f"### 模型：{r.model_name}（失败）\n{r.error}")
        header = f"[AgentParliament]{len(parallel.successful)}/{len(parallel.results)}个模型成功完成。"
        usage = _fmt_tokens(parallel.total_input_tokens, parallel.total_output_tokens)
        if usage:
            header += f" 总用量：{usage}"
        header += "\n以下是各模型的独立回答，请由链的下一步或综合步骤对比共识与分歧。"
        return {
            "ok": True, "text": f"{header}\n\n" + "\n\n---\n\n".join(sections),
            "model_used": "parallel", "cost_usd": parallel.total_cost_usd,
            "input_tokens": parallel.total_input_tokens, "output_tokens": parallel.total_output_tokens,
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
                allowed_tools=list(_RESEARCH_TOOLS_LOCAL),
                permission_mode="default",
                disallowed_tools=_DISALLOWED_TOOLS,
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
        # 顾问阶段是纯文本融合（task + draft.text），走 API 直连
        try:
            advisor = await asyncio.to_thread(
                run_with_chain_api,
                config=config, role=advisor_role, prompt=memory + advisor_prompt,
                cwd=cwd,
            )
        except ProfileError as exc:
            # 强模型升级失败：返回草稿并标注求助失败（ok=True，因为有草稿可用）
            return {
                "ok": True, "text": draft.text,
                "model_used": draft.model_used, "cost_usd": draft.total_cost_usd,
                "input_tokens": draft.total_input_tokens, "output_tokens": draft.total_output_tokens,
                "error": f"强模型升级失败，返回草稿：{exc}",
            }
        if not advisor.ok:
            return {
                "ok": True, "text": draft.text,
                "model_used": draft.model_used, "cost_usd": draft.total_cost_usd,
                "input_tokens": draft.total_input_tokens, "output_tokens": draft.total_output_tokens,
                "error": "顾问模型失败，返回草稿",
            }
        return {
            "ok": True,
            "text": "【阶段1：中等模型草稿】\n" + draft.text + "\n\n【阶段2：强模型顾问意见】\n" + advisor.text,
            "model_used": advisor.model_used, "cost_usd": draft.total_cost_usd + advisor.total_cost_usd,
            "input_tokens": draft.total_input_tokens + advisor.total_input_tokens,
            "output_tokens": draft.total_output_tokens + advisor.total_output_tokens,
        }

    if tool_name == "verify_implementation":
        # Tier 2：在 git worktree 隔离副本内设计并执行测试，产出 ground truth
        # task 是 verify_implementation 的主参数（advisor_analysis 也用 task，这里语义一致）
        verify_task = task or question or original_task
        if not verify_task:
            return _chain_error("verify_implementation 缺少 task 参数（要验证的任务描述）")

        # 创建 worktree（失败则返回错误，不阻断链路）
        try:
            wt = WorktreeSession(cwd)
            wt_path = wt.create()
        except WorktreeError as exc:
            return _chain_error(f"worktree 创建失败：{exc}")

        # 在 worktree 内启动子 Agent（default + Tier 2 工具集）
        v_prompt = _build_verify_implementation_prompt(verify_task, "", "")
        try:
            result = await asyncio.to_thread(
                run_with_chain,
                config=config, role=role, prompt=memory + v_prompt,
                cwd=wt_path,
                allowed_tools=list(_VERIFY_TOOLS),
                permission_mode="default",
                disallowed_tools=_VERIFY_DISALLOWED_TOOLS,
                dangerously_skip_permissions=True,
            )
        except ProfileError as exc:
            wt.destroy()
            return _chain_error(str(exc))

        # 提取建议性 diff
        suggested_diff = ""
        try:
            suggested_diff = wt.get_diff()
        except Exception:
            pass

        # 销毁 worktree
        wt.destroy()

        if not result.ok:
            return _chain_result_from_run(result)

        # 快照状态标注：与 server.py 的 verify_implementation handler 保持一致
        # 让链路结果也能反映"验证的是当前真实代码还是仅已提交代码"
        if wt.snapshot_used:
            snapshot_note = "[验证基准] 已包含主仓库未提交改动，验证的是当前真实代码状态。\n\n"
        else:
            snapshot_note = (
                "[验证基准] ⚠️ 快照未生成，本次基于最近一次提交（HEAD）验证，"
                "未包含工作区未提交改动，若验证尚未 commit 的实现结论可能对着旧代码。\n\n"
            )

        # 拼接结论 + 建议性 diff
        text = snapshot_note + result.text
        if suggested_diff.strip():
            text += (
                "\n\n---- 建议性 diff（子 Agent 在隔离副本内的改动，交主 Agent 审批是否合并）----\n"
                f"```diff\n{suggested_diff}\n```"
            )
        return {
            "ok": True, "text": text,
            "model_used": result.model_used, "cost_usd": result.total_cost_usd,
            "input_tokens": result.total_input_tokens, "output_tokens": result.total_output_tokens,
        }

    # 不可达防护（_ALLOWED_CHAIN_TOOLS 校验已确保不会到达这里）
    return _chain_error(f"未知工具：{tool_name}")

# delegate_chain 校验 stage 参数用的合法参数集合，与 _call_tool_by_name 签名自动同步
_CALL_TOOL_KWARGS = frozenset(inspect.signature(_call_tool_by_name).parameters) - {"tool_name"}

def calculate_chain_timeout(
    stages: list[dict], config: Config,
    synthesizer_role: str | None = None,
) -> float:
    """
    计算delegate_chain的动态总超时阈值

    公式：total = Σ stage_timeout_i + optional synth_timeout
    其中 stage_timeout_i = base_timeout(stage_i.role) × chain_overhead(role_i) × (1.0 + i × INFLATION_FACTOR)
    chain_overhead = min(角色链长度, 2)（保守估算最多降级一次，首选模型通常可用）

    设计思路：
    - 越靠后的步骤要处理越多累积信息（__PREVIOUS_RESULT__），实际耗时越长
    - 前面的步骤：系数 ≈ 1.0（直接用基础超时）
    - 后面的步骤：系数线性递增（1.0 + i × 0.15）
    - 角色链重试开销：失败降级时串行尝试多个模型，保守估算最多降级一次
    - 基础超时取自 profiles.json 的 role_overrides / timeout_seconds
    """
    total = 0.0
    for i, stage in enumerate(stages):
        role = stage.get("role") or "third_party"
        base_timeout = config.timeout_for(role)
        # 角色链重试开销：min(链长, 2) 表示保守估算最多降级一次
        role_chain_len = len(config.roles.get(role, []))
        chain_overhead = min(role_chain_len, 2)
        coefficient = 1.0 + i * INFLATION_FACTOR
        total += base_timeout * chain_overhead * coefficient

    # synthesize 步骤位于所有 stages 之后，系数最高
    if synthesizer_role:
        synth_base = config.timeout_for(synthesizer_role)
        synth_chain_len = len(config.roles.get(synthesizer_role, []))
        synth_overhead = min(synth_chain_len, 2)
        synth_coefficient = 1.0 + len(stages) * INFLATION_FACTOR
        total += synth_base * synth_overhead * synth_coefficient

    return total

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
        # synthesize 综合步骤是纯文本融合（task + 各步 text），走 API 直连省去 CLI 子进程开销
        # memory_block 是宿主进程预先读好的字符串，作为纯文本输入而非指令子进程读文件
        result = await asyncio.to_thread(
            run_with_chain_api,
            config=_get_config(),
            role=aggregator_role,
            prompt=_memory_block(cwd) + synth_prompt,
            cwd=cwd,
        )
    except ProfileError as exc:
        return _chain_error(str(exc))

    if not result.ok:
        return _chain_error(
            "综合模型失败", cost_usd=result.total_cost_usd,
            input_tokens=result.total_input_tokens, output_tokens=result.total_output_tokens,
        )
    return {
        "ok": True, "text": result.text, "model_used": result.model_used, "cost_usd": result.total_cost_usd,
        "input_tokens": result.total_input_tokens, "output_tokens": result.total_output_tokens,
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

    # 计算动态总超时阈值（含膨胀系数）
    config = _get_config()
    total_timeout = calculate_chain_timeout(
        stages, config,
        synthesizer_role=aggregator_role if synthesize else None,
    )

    results: list[dict] = []
    total_cost = 0.0
    total_in = 0
    total_out = 0
    chain_start = time.monotonic()

    for i, stage in enumerate(stages):
        # 动态超时检查：每一步开始前判断总耗时是否已超过阈值
        elapsed = time.monotonic() - chain_start
        if elapsed >= total_timeout:
            return _render_chain_result(
                task, results, total_cost,
                project_dir_warning=project_dir_warning,
                timeout_info=(
                    f"链路在第 {i+1} 步前主动终止：累计耗时 {elapsed:.1f}s 超过动态阈值 {total_timeout:.0f}s"
                    f"（{len(stages)}步链路，膨胀系数 {INFLATION_FACTOR}）。"
                    f"已完成 {i}/{len(stages)} 步。"
                ),
                total_input_tokens=total_in, total_output_tokens=total_out,
            )

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
        if tool_name == "verify_implementation" and not tool_kwargs.get("task") and not tool_kwargs.get("question") and not tool_kwargs.get("original_task"):
            # 未提供 task 时用链路整体 task 自动填充
            tool_kwargs["task"] = task

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
        elif tool_name == "verify_implementation":
            # verify 用 task 字段，自动填充逻辑与 advisor 一致；enriched 追加为补充说明
            if "task" not in tool_kwargs:
                tool_kwargs["task"] = f"{task}\n\n{enriched}" if enriched else task
            elif enriched:
                tool_kwargs["task"] = f"{tool_kwargs['task']}\n\n{enriched}"
            # focus 可由 enriched 补充（若 stage 未指定 focus）
            if enriched and "focus" not in tool_kwargs:
                tool_kwargs["focus"] = enriched

        step_result = await _call_tool_by_name(
            tool_name, project_dir=project_dir, role=stage_role,
            memory_override=chain_memory, **tool_kwargs,
        )
        results.append(step_result)
        total_cost += step_result.get("cost_usd", 0.0)
        total_in += step_result.get("input_tokens", 0) or 0
        total_out += step_result.get("output_tokens", 0) or 0

        if not step_result["ok"]:
            return _render_chain_result(
                task, results, total_cost, failed_at=i, project_dir_warning=project_dir_warning,
                total_input_tokens=total_in, total_output_tokens=total_out,
            )

    # synthesize 综合步骤（也受动态超时约束）
    if synthesize:
        elapsed = time.monotonic() - chain_start
        if elapsed >= total_timeout:
            return _render_chain_result(
                task, results, total_cost,
                project_dir_warning=project_dir_warning,
                timeout_info=(
                    f"链路在 synthesize 步骤前主动终止：累计耗时 {elapsed:.1f}s 超过动态阈值 {total_timeout:.0f}s"
                    f"（{len(stages)}步链路，膨胀系数 {INFLATION_FACTOR}）。"
                    f"已完成 {len(stages)}/{len(stages)} 步，综合步骤未执行。"
                ),
                total_input_tokens=total_in, total_output_tokens=total_out,
            )
        synth_result = await _run_synthesize(task, results, project_dir, aggregator_role)
        results.append(synth_result)
        total_cost += synth_result.get("cost_usd", 0.0)
        total_in += synth_result.get("input_tokens", 0) or 0
        total_out += synth_result.get("output_tokens", 0) or 0
        if not synth_result["ok"]:
            return _render_chain_result(
                task, results, total_cost, failed_at=len(stages), project_dir_warning=project_dir_warning,
                total_input_tokens=total_in, total_output_tokens=total_out,
            )

    return _render_chain_result(
        task, results, total_cost, project_dir_warning=project_dir_warning,
        total_input_tokens=total_in, total_output_tokens=total_out,
    )


# ─── delegate_dialogue session 管理与编排 ────────────────────────────
# 跨MCP调用接力的对话上下文：子Agent提问后存session，主Agent带session_id续答。
# 内存态，进程重启即失（对话是临时态）；TTL兜底清理；无外部资源故无需atexit。
# 与 _NEED_ADVISOR_TAG 的哨兵模式同源，区别在此处是"中途向主Agent提问并继续同一任务"。

_DIALOGUE_SESSIONS: dict[str, dict] = {}
_DIALOGUE_LOCK = threading.Lock()
_DIALOGUE_SESSION_TTL = 600  # 10分钟，对话周期上限

def _cleanup_expired_sessions_locked() -> None:
    """ 清理过期session（调用方须持 _DIALOGUE_LOCK） """
    now = time.monotonic()
    expired = [
        sid for sid, s in _DIALOGUE_SESSIONS.items()
        if now - s.get("created_at", now) > _DIALOGUE_SESSION_TTL
    ]
    for sid in expired:
        _DIALOGUE_SESSIONS.pop(sid, None)

def _new_session(
    question: str, project_dir: str, cwd: str, role: str,
    context_files: list[str] | None, allow_web: bool,
    memory: str, config,
) -> str:
    """ 新建对话session，返回 session_id """
    sid = uuid.uuid4().hex[:12]
    with _DIALOGUE_LOCK:
        _cleanup_expired_sessions_locked()
        _DIALOGUE_SESSIONS[sid] = {
            "question": question,
            "project_dir": project_dir,
            "cwd": cwd,
            "role": role,
            "context_files": context_files,
            "allow_web": allow_web,
            "memory": memory,
            "config": config,
            "prev_conclusion": "",
            "turns": 0,
            "created_at": time.monotonic(),
            "history": [],  # [{"turn": int, "question": str, "answer": str}]
        }
    return sid

def _get_session(session_id: str) -> dict | None:
    """ 取session，不存在/过期返回None；惰性清理过期 """
    with _DIALOGUE_LOCK:
        _cleanup_expired_sessions_locked()
        return _DIALOGUE_SESSIONS.get(session_id)

def _update_session(session_id: str, **fields) -> None:
    """ 锁内更新session字段 """
    with _DIALOGUE_LOCK:
        s = _DIALOGUE_SESSIONS.get(session_id)
        if s is not None:
            s.update(fields)

def _append_history(session_id: str, entry: dict) -> None:
    """ 锁内向session.history追加一条记录 """
    with _DIALOGUE_LOCK:
        s = _DIALOGUE_SESSIONS.get(session_id)
        if s is not None:
            s["history"].append(entry)

def _set_history_answer(session_id: str, answer: str) -> None:
    """ 锁内补全history最后一条的answer（续答时调用） """
    with _DIALOGUE_LOCK:
        s = _DIALOGUE_SESSIONS.get(session_id)
        if s is not None and s["history"]:
            s["history"][-1]["answer"] = answer

def _close_session(session_id: str) -> dict | None:
    """ 关闭session并返回其内容（供结束分支取history）；不存在返回None """
    with _DIALOGUE_LOCK:
        return _DIALOGUE_SESSIONS.pop(session_id, None)

def _get_session_turns(session_id: str) -> int:
    """ 取session已记录的轮次（新建后0，续答前是上轮值） """
    with _DIALOGUE_LOCK:
        s = _DIALOGUE_SESSIONS.get(session_id)
        return s["turns"] if s else 0

async def _execute_delegate_dialogue(
    question: str,
    project_dir: str,
    role: str,
    context_files: list[str] | None,
    allow_web: bool,
    session_id: str,
    answer: str,
    max_dialogue: int,
) -> str:
    """
    delegate_dialogue 编排：新建/续答分流 + 子Agent一轮 + 提问检测/续答接力
    - 新建：question+project_dir必填，建session，跑首轮，有提问则存上下文返回提问
    - 续答：session_id+answer，取上下文，截断prev_conclusion，跑下一轮，提问或结束
    - max_dialogue上限：达上限时强行取当前结论结束，防无限循环
    """
    if not isinstance(max_dialogue, int) or isinstance(max_dialogue, bool) or max_dialogue < 1:
        return "[AgentParliament]delegate_dialogue 调用错误：max_dialogue 必须是大于等于 1 的整数。"

    is_continue = bool(session_id)
    project_dir_warning = "" if is_continue else _project_dir_warning(project_dir)

    # —— 分流 ——
    if is_continue:
        session = _get_session(session_id)
        if session is None:
            return (
                "[AgentParliament]delegate_dialogue 会话已过期或不存在"
                "（session TTL=600s 或已关闭），请重新开始一次新的 delegate_dialogue 调用。"
            )
        if not answer:
            return (
                f"[AgentParliament]delegate_dialogue 续答需传 answer 参数"
                f"（session_id={session_id}），请回答上一轮子Agent的提问后再次调用。"
            )
        # 续答前先补全上一轮提问的回答到history
        _set_history_answer(session_id, answer)
        # 达上限：不继续，返回上轮结论
        if session["turns"] >= max_dialogue:
            _close_session(session_id)
            return _format_dialogue_final(
                session["prev_conclusion"], None, None, None,
                session["history"], project_dir_warning,
            ) + "\n\n（已达对话上限，取子Agent上一轮结论）"
        # 截断prev_conclusion防续答轮prompt膨胀导致单轮超时
        truncated_prev = _truncate_for_chain(session["prev_conclusion"])
        truncated = truncated_prev != session["prev_conclusion"]
        prompt = _build_dialogue_continue_prompt(truncated_prev, answer, session["question"])
        memory = session["memory"]
        cwd = session["cwd"]
        config = session["config"]
        role = session["role"]
        allow_web = session["allow_web"]
    else:
        if not question:
            return "[AgentParliament]delegate_dialogue 调用错误：首次调用 question 必填。"
        if not project_dir:
            return (
                "[AgentParliament]delegate_dialogue 调用错误：project_dir 必填"
                "（对话需读取项目CLAUDE.md记忆体，不传则副Agent无上下文）。"
            )
        try:
            cwd = _resolve_cwd(project_dir)
            config = _get_config()
        except ProfileError as exc:
            return f"[AgentParliament] 配置或调用错误：{exc}"
        memory = _memory_block(cwd)
        prompt = _build_dialogue_prompt(question, context_files)
        session_id = _new_session(
            question, project_dir, cwd, role, context_files, allow_web, memory, config,
        )
        truncated = False

    # —— 跑一轮子Agent（default + 只读白名单，与delegate_research一致）——
    allowed = list(_RESEARCH_TOOLS_WEB if allow_web else _RESEARCH_TOOLS_LOCAL)
    try:
        result = await asyncio.to_thread(
            run_with_chain,
            config=config, role=role, prompt=memory + prompt,
            cwd=cwd, allowed_tools=allowed,
            permission_mode="default", disallowed_tools=_DISALLOWED_TOOLS,
        )
    except ProfileError as exc:
        if not is_continue:
            _close_session(session_id)  # 新建失败清理空session
        return f"[AgentParliament] 配置或调用错误：{exc}"
    except Exception as exc:
        # 兜底：非预期异常（CancelledError/KeyboardInterrupt/等）也清理session，避免泄漏至TTL
        if not is_continue:
            _close_session(session_id)
        return f"[AgentParliament] 调用错误：{exc}"

    # 失败：不更新prev_conclusion（续答保留上轮供重试），返回失败渲染
    if not result.ok:
        if not is_continue:
            _close_session(session_id)
        return project_dir_warning + _format_result(result)

    # —— 检测提问标记 ——
    has_ask, ask_question, conclusion = detect_ask_parent(result.text)
    prev_turns = _get_session_turns(session_id)  # 新建后0，续答前是上轮值
    new_turns = prev_turns + 1

    if has_ask and new_turns < max_dialogue:
        # 有提问且未达上限：存上下文供续答
        _update_session(session_id, prev_conclusion=conclusion, turns=new_turns)
        _append_history(session_id, {"turn": new_turns, "question": ask_question, "answer": ""})
        return _format_ask_response(
            session_id, ask_question, conclusion, new_turns, max_dialogue, result.model_used,
            result.total_input_tokens, result.total_output_tokens,
        )

    # 无提问或达上限：先记录达上限轮的未回答提问，再取history关闭
    reached_limit = has_ask and new_turns >= max_dialogue
    if reached_limit:
        _append_history(session_id, {"turn": new_turns, "question": ask_question, "answer": "(达上限未回答)"})
    closed = _close_session(session_id)
    history = closed["history"] if closed else []
    suffix = "\n\n（已达对话上限 max_dialogue，子Agent仍想提问，取其当前结论）" if reached_limit else ""
    return _format_dialogue_final(
        conclusion, result.model_used, result.total_input_tokens, result.total_output_tokens,
        history, project_dir_warning, truncated=truncated,
    ) + suffix
