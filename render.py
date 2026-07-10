"""
render.py——AgentParliament的结果渲染与结构校验

职责：
1. 把runner.RunResult/ParallelResult渲染成给主Agent阅读的文本，诚实标注降级与成本
2. 校验peer_review(structured=True)的JSON输出
3. 渲染delegate_chain的链路结果
4. project_dir未传时的警告注入

纯叶子模块：只import runner的数据类型，不持有全局可变状态。
"""
from __future__ import annotations

import json

from runner import RunResult, ParallelResult

# structured review输出中每个问题对象必须包含的字段
_REQUIRED_REVIEW_KEYS = {"severity", "file", "line", "description", "suggestion"}

# helper：截断超长__PREVIOUS_RESULT_
_CHAIN_RESULT_TRUNCATE_THRESHOLD = 10000

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

def _validate_structured_review(text: str) -> tuple[bool, str, str]:
    """
    尽力解析peer_review(structured=True)的输出
    返回(ok, cleaned_json_or_raw, message)：
    - ok=True：解析成功，cleaned_json为干净JSON数组文本
    - ok=False：解析失败，cleaned_json为原始文本，供主Agent人工 salvage，message 为失败原因
    设计取舍：不自动重试，解析失败时仍返回原始文本，不隐藏失败信号
    """
    raw = text.strip()

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

def _truncate_for_chain(text: str) -> str:
    """ 超长时摘要截断，返回带省略标记的短文本 """
    if len(text) <= _CHAIN_RESULT_TRUNCATE_THRESHOLD:
        return text
    head = text[:500]
    tail = text[-500:]
    return (
        f"{head}\n…（中间省略 {len(text) - 1000} 字符）…\n{tail}"
    )

def _chain_result_from_run(result: RunResult, text_override: str | None = None) -> dict:
    """
    把RunResult转成_call_tool_by_name的标准返回dict
    text_override用于peer_review的structured清洗等需要替换文本的场景
    """
    return {
        "ok": result.ok,
        "text": (text_override if text_override is not None else result.text) if result.ok else "",
        "model_used": result.model_used,
        "cost_usd": result.total_cost_usd,
        "error": "" if result.ok else "所有模型调用失败",
    }

def _chain_error(error: str, cost_usd: float = 0.0) -> dict:
    """构造失败返回 dict"""
    return {
        "ok": False, "text": "", "model_used": None,
        "cost_usd": cost_usd, "error": error,
    }

def _render_chain_result(
    task: str,
    results: list[dict],
    total_cost: float,
    failed_at: int | None = None,
    project_dir_warning: str = "",
    timeout_info: str = "",
) -> str:
    """ 渲染delegate_chain结果给主Agent """
    n = len(results)

    # 超时终止：步骤未执行即被跳过，不是"失败"，需要独立 header 措辞
    if timeout_info:
        header = f"[AgentParliament]delegate_chain 超时终止，已完成 {n} 个步骤。"
    elif failed_at is None:
        header = f"[AgentParliament]delegate_chain 完成，共 {n} 个步骤，全部成功。"
    else:
        header = f"[AgentParliament]delegate_chain 在第 {failed_at + 1} 步失败，已完成 {failed_at} 个步骤。"

    if total_cost > 0:
        header += f" 总成本：${total_cost:.4f}"

    # 超时详情追加在 header 后
    if timeout_info:
        header += f"\n{timeout_info}"

    # 最终结论：最后一步的输出
    final_text = results[-1].get("text", "") if results else ""
    sections = [f"最终结论：\n{final_text}"]

    # 各步骤详情
    for i, r in enumerate(results):
        status_ok = r.get("ok", False)
        model = r.get("model_used", "?")
        cost = r.get("cost_usd", 0.0)
        text = r.get("text", "")
        error = r.get("error", "")
        # 限制每步摘要长度，避免结果过长
        summary = text[:500] + "..." if len(text) > 500 else text
        tag = "✅" if status_ok else "❌"
        section = f"【步骤{i+1}：{model} | ${cost:.4f} {tag}】\n{summary}"
        if error:
            section += f"\n错误：{error}"
        sections.append(section)

    return project_dir_warning + header + "\n\n" + "\n\n".join(sections)


# ─── delegate_dialogue 渲染 ──────────────────────────────────────────

def _format_ask_response(
    session_id: str,
    question: str,
    prev_conclusion: str,
    turn: int,
    max_dialogue: int,
    model_used: str | None,
    cost_usd: float = 0.0,
) -> str:
    """ 渲染 delegate_dialogue 的提问返回：含子Agent当前结论 + 提问 + 续答指引 """
    model_tag = f"（模型 `{model_used}`）" if model_used else ""
    cost_tag = f" 本轮成本：${cost_usd:.4f}" if cost_usd > 0 else ""
    return (
        f"[AgentParliament] 子Agent请求确认（对话轮次 {turn}/{max_dialogue}）{model_tag}{cost_tag}：\n"
        f"待确认问题：{question}\n"
        f"———— 子Agent当前结论 ————\n{prev_conclusion}\n"
        f"请再次调用 delegate_dialogue，传 session_id={session_id} 与 answer=<你的回答> "
        f"让子Agent继续。若需结束对话，不再调用即可（session 将在 TTL 后自动清理）。"
    )

def _format_dialogue_final(
    final_text: str,
    model_used: str | None,
    cost_usd: float,
    history: list[dict],
    project_dir_warning: str = "",
    truncated: bool = False,
) -> str:
    """
    渲染 delegate_dialogue 的最终返回：最终结论 + 对话历史
    history: [{"turn": int, "question": str, "answer": str}, ...]
    """
    header = "[AgentParliament]对话完成"
    if model_used:
        header += f"，由模型`{model_used}`给出最终结论"
    if cost_usd > 0:
        header += f" 本次成本：${cost_usd:.4f}"

    parts = [header + "\n\n" + final_text]
    if truncated:
        parts.append("（注：上轮结论过长已截断，子Agent基于截断版本继续）")
    if history:
        hist_lines = ["———— 对话历史 ————"]
        for h in history:
            hist_lines.append(f"【轮{h['turn']}】提问：{h['question']} → 回答：{h['answer']}")
        parts.append("\n".join(hist_lines))
    return project_dir_warning + "\n\n".join(parts)
