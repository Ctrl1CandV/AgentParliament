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

# Windows命令行参数长度安全上限，diff超过此值时拒绝内嵌传递
_DIFF_SIZE_LIMIT = 20000

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
            lines.append(f"  - {attempt.model_name}: {attempt.error}")
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
) -> str:
    """
    委托一个只读副Agent对代码库做调研，并返回它的结论。
    适用场景：问题较复杂、需要第二个模型的视角，或想把调研从主会话剥离以保持主上下文干净。
    注意副Agent只读本地代码，不会改文件。

    Args:
        question: 要副Agent回答的调研问题，应尽量具体。
        role: 使用profiles.json中的哪条角色失败链，默认"researcher"。
        project_dir: 副Agent的工作目录，项目根目录。不传则用MCP所在目录。
        context_files: 可选，主Agent已定位的相关文件路径列表。会拼进提示词，引导副Agent优先阅读这些文件，避免从头扫描整个项目造成浪费。

    Returns:
        副Agent的调研结论文本；若发生降级会在开头注明。
    """
    prompt = question
    if context_files:
        joined = "\n".join(f"- {p}" for p in context_files)
        prompt = (
            f"{question}\n\n"
            f"请优先阅读以下相关文件，无需扫描整个项目：\n{joined}"
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

# 工具 2：peer_review
@mcp.tool()
def peer_review(
    diff: str, role: str = "reviewer",
    focus: str = "", project_dir: str | None = None,
) -> str:
    """
    委托一个只读副Agent审查一段代码改动，返回按严重程度分级的问题列表
    适用场景：主Agent完成实现后，想要另一个模型独立核查。审查只需要diff， token占用远小于全量文件。副Agent只读，不会改文件。

    Args:
        diff: 待审查的改动，通常是`git diff`的输出。大小限制约20KB，超过请改用文件路径
        role: 使用哪条角色失败链，默认"reviewer"
        focus: 可选，审查重点（如"安全性""并发""边界条件"），留空则做全面审查
        project_dir: 副Agent的工作目录，便于它在需要时阅读改动涉及的上下文文件

    Returns:
        分级的审查结论文本；若发生降级会在开头注明
    """
    if len(diff) > _DIFF_SIZE_LIMIT:
        return (
            f"[AgentParliament]diff大小为 {len(diff)} 字符，"
            f"超过安全上限{_DIFF_SIZE_LIMIT}。"
            f"请将diff写入临时文件后通过context_files参数传入文件路径，"
            f"或只传关键改动的diff。"
        )

    focus_line = f"本次审查重点：{focus}\n\n" if focus else ""
    prompt = (
        "你是一名严谨的代码审查者。请审查下面的代码改动，按严重程度分级列出问题："
        "🔴 严重 / 🟠 重要 / 🟡 次要 / 🟢 建议。"
        "对每个问题指出所在位置、原因和修复建议。若改动无明显问题也请明确说明。\n\n"
        f"{focus_line}"
        f"待审查的改动（diff）：\n```diff\n{diff}\n```"
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
        "你是一名独立审计者，你的职责是批判性地审视另一个模型给出的结论，"
        "找出其中可能存在的逻辑漏洞、盲区或过度推断。\n\n"
        "请按以下结构输出：\n"
        "1.【结论概要】用1-2句话概括原结论的核心观点\n"
        "2.【共识】列出你与原结论一致的部分\n"
        "3.【分歧】列出你与原结论不一致的部分，对每个分歧说明：\n"
        "   - 原结论说了什么\n"
        "   - 你为什么认为这可能有问题\n"
        "   - 你的替代判断是什么\n"
        "4.【盲区】原结论可能遗漏了哪些重要维度\n"
        "5.【总体评价】原结论整体是否可靠（可靠/部分可靠/不可靠），以及你的理由\n\n"
        f"原始任务：{original_task}\n\n"
        f"已有结论：{existing_result}"
        f"{concern_block}"
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
        "请按以下结构输出：\n"
        "1.【方案概要】用1-2句话概括方案的核心思路\n"
        "2.【潜在风险】按严重程度列出方案可能遇到的问题：\n"
        "   🔴 致命风险（会导致方案完全不可行）\n"
        "   🟠 重大风险（会导致方案部分失败或需要大幅调整）\n"
        "   🟡 一般风险（需要额外处理但可控）\n"
        "3.【遗漏场景】方案可能没有考虑到的边界情况或使用场景\n"
        "4.【改进建议】针对发现的风险，给出具体的改进方向\n"
        "5.【总体判断】方案是否值得推进（推荐/有条件推荐/不推荐），以及理由\n\n"
        f"待验证的方案：{approach}"
        f"{goal_block}"
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


def main() -> None:
    """ 以stdio方式启动MCP服务，供命令行入口（pyproject.toml的scripts）与直接运行共用 """
    mcp.run()

if __name__ == "__main__":
    main()
