"""
prompts.py——AgentParliament的prompt构造与共享记忆体注入

职责：
1. 构造8个原子工具的完整prompt（纯函数，供server.py的工具handler与chain.py的_call_tool_by_name复用，
   保证单工具调用与链内调用产出完全一致）
2. 读取副Agent工作目录下的CLAUDE.md/SPEC.md/docs/adr，渲染成注入prompt开头的记忆块
   （显式注入而非依赖Claude Code自动加载，确保headless模式下副Agent确定性地获得项目上下文）

本模块是纯叶子模块：不import任何项目内其他模块，不持有全局可变状态。
"""
from __future__ import annotations

from pathlib import Path
import re

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

# 压制plan/default模式下副Agent"停下来等待审批"的行为
_NO_HALT_NOTICE = (
    "\n\n[执行要求] 请一次性完成全部调研并直接在回复中输出最终结论。"
    "不要提交计划等待审批，不要中途停下来询问是否继续，"
    "不要输出『等待审阅』或『请确认』之类的话。"
)

# advisor_analysis触发升级的哨兵标记，中等模型在草稿末尾输出此标记表示「我不确定，请求助强模型」
_NEED_ADVISOR_TAG = "[NEED_ADVISOR]"

# 共享记忆体文件名。副Agent通过MCP显式读取并注入，从而确定性地获得项目的领域语言、已定架构决策与硬约束
# 不依赖Claude Code在headless（-p）模式下是否会自动加载CLAUDE.md
_MEMORY_FILENAME = "CLAUDE.md"

# 注入记忆体的字符上限，防止异常膨胀的记忆体挤占prompt预算；超出时截断并提示
_MEMORY_CHAR_LIMIT = 16000

# SPEC.md和ADR增量注入的字符上限，防止挤占prompt预算
_SPEC_BRIEF_LIMIT, _ADR_INDEX_LIMIT = 800, 400

# SPEC.md固定二级标题名，用于标题匹配提取进度/下一步
_SPEC_HEADINGS = ("## 执行方案", "## 进度", "## 下一步")

# 匹配 CLAUDE.md中的ADR索引行，如"[ADR-001]选用asyncio.to_thread解决事件循环阻塞"
_ADR_INDEX_PATTERN = re.compile(r"^\[ADR-\d+[^\]]*\].*$", re.MULTILINE)

def _extract_spec_brief(cwd: str) -> str:
    """
    从 SPEC.md 提取最新进度末条 + 下一步首行，作为轻量执行上下文注入副Agent
    用固定二级标题（## 进度 / ## 下一步）做匹配，而非脆弱的行数截取
    best-effort：文件不存在、标题不匹配、读取失败都返回空串
    """
    spec_path = Path(cwd) / "SPEC.md"
    try:
        if not spec_path.is_file():
            return ""
        text = spec_path.read_text(encoding="utf-8")
    except Exception:
        return ""

    lines = text.splitlines()
    sections: dict[str, list[str]] = {}
    current = None
    for line in lines:
        # 检测二级标题行
        if line.strip().startswith("## "):
            heading = line.strip()
            if heading in _SPEC_HEADINGS:
                current = heading
                sections[current] = []
            else:
                current = None  # 遇到非约定标题，退出当前段
        elif current is not None:
            sections[current].append(line)

    parts: list[str] = []

    # 进度段：取最后一条非空列表项
    progress_lines = sections.get("## 进度", [])
    # 从末尾向前找最后一条非空、非纯标线的列表项
    last_progress = ""
    for line in reversed(progress_lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("---") and not stripped.startswith("###"):
            # 优先取列表项（- [x] 或 - [ ] 开头），也接受普通非空行
            if stripped.startswith("- ") or stripped.startswith("- ["):
                last_progress = stripped
                break
            # 如果不是列表项但非空，也作为 fallback
            if not last_progress:
                last_progress = stripped
    if last_progress:
        parts.append(f"[执行进度] {last_progress}")

    # 下一步段：取第一条非空、非纯标线行
    next_lines = sections.get("## 下一步", [])
    first_next = ""
    for line in next_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("---") and not stripped.startswith("###"):
            first_next = stripped
            break
    if first_next:
        parts.append(f"[下一步] {first_next}")

    if not parts:
        return ""

    brief = " | ".join(parts)
    if len(brief) > _SPEC_BRIEF_LIMIT:
        brief = brief[:_SPEC_BRIEF_LIMIT] + "…（截断）"
    return f"\n[执行上下文] {brief}\n"

def _extract_adr_index(cwd: str, claude_md_content: str) -> str:
    """
    提取ADR索引，优先从CLAUDE.md内容中提取[ADR-XXX]索引行，无索引时fallback到docs/adr/目录列文件名
    best-effort：无索引、无目录都返回空串
    """
    # 优先从 CLAUDE.md 提取 ADR 索引行
    matches = _ADR_INDEX_PATTERN.findall(claude_md_content)
    if matches:
        index_text = "; ".join(matches)
        if len(index_text) > _ADR_INDEX_LIMIT:
            index_text = index_text[:_ADR_INDEX_LIMIT] + "…（完整列表见 docs/adr/）"
        return f"\n[架构决策] {index_text}\n"

    # fallback：扫描 docs/adr/ 目录
    adr_dir = Path(cwd) / "docs" / "adr"
    try:
        if not adr_dir.is_dir():
            return ""
        adr_files = sorted(adr_dir.glob("*.md"))
        if not adr_files:
            return ""
        names = [f.stem for f in adr_files]  # 文件名去 .md 后缀
        index_text = "; ".join(names)
        if len(index_text) > _ADR_INDEX_LIMIT:
            index_text = index_text[:_ADR_INDEX_LIMIT] + "…（完整列表见 docs/adr/）"
        return f"\n[架构决策] 可查阅 docs/adr/：{index_text}\n"
    except Exception:
        return ""

def _memory_block(cwd: str) -> str:
    """
    从副Agent工作目录读取共享记忆体，渲染成注入prompt开头的记忆块
    同时追加SPEC.md执行上下文和ADR 索引，形成回读闭环
    设计要点：
    - 显式读取而非依赖Claude Code自动加载，确保被调用的副Agent确定性地获得项目记忆
    - 记忆体是best-effort：文件不存在或读取失败都返回空串，绝不让记忆问题阻断工具主流程
    - 超过字符上限时截断，避免异常膨胀的记忆体挤占prompt
    - SPEC/ADR增量是注入到prompt文本中，不是指令副Agent去读文件，不与_MATERIAL_NOTICE冲突
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

    # 追加SPEC.md执行上下文和ADR索引
    spec_brief = _extract_spec_brief(cwd)
    adr_index = _extract_adr_index(cwd, content)

    return (
        "[项目记忆] 以下是本项目的共享记忆体（CLAUDE.md），"
        "包含领域语言、已定架构决策、硬约束与当前状态。\n"
        "请在工作时沿用其中的术语、遵守其中的约束，不要与已定决策冲突；"
        "若你的分析与记忆体存在矛盾，请明确指出而非默默忽略。\n"
        "————（项目记忆开始）————\n"
        f"{content}\n"
        "————（项目记忆结束）————\n"
        f"{spec_brief}{adr_index}\n"
    )

# 提取后的纯prompt构造函数，供handler和delegate_chain复用，保证单工具调用与chain内调用产出完全一致
def _build_research_prompt(question: str, context_files: list[str] | None) -> str:
    """ 构造delegate_research完整prompt """
    prompt = question
    if context_files:
        joined = "\n".join(f"- {p}" for p in context_files)
        prompt = (
            f"{question}\n\n"
            f"请优先阅读以下相关文件，无需扫描整个项目：\n{joined}"
        )
    prompt += (
        "\n\n请按以下结构输出调研结论：\n"
        "1.【问题复述】用自己的话复述调研问题，确认理解无误\n"
        "2.【发现】列出你的调研发现，按重要性排序\n"
        "3.【依据】每条发现附上支撑证据（代码位置、文档引用等）\n"
        "4.【结论】对原始问题的直接回答\n"
        "5.【局限】本次调研可能遗漏的方面"
    )
    prompt += _NO_HALT_NOTICE
    return prompt

def _build_review_prompt(
    diff: str,
    focus: str,
    context_files: list[str] | None,
    structured: bool,
) -> tuple[str, str, str]:
    """
    构造peer_review完整prompt的三个片段，返回(prefix, focus_line, instruction)
    prefix已包含_MATERIAL_NOTICE / 上下文提示，可直接拼接 instruction/focus_line/diff
    侧效应：不计算extra_dirs
    """
    prefix = _MATERIAL_NOTICE
    if context_files:
        joined = "\n".join(f"- {p}" for p in context_files)
        prefix = (
            "[上下文]以下文件与本次改动相关，请在审查时阅读它们，"
            "理解跨文件交互、接口契约与调用方影响，不要孤立地只看diff：\n"
            f"{joined}\n\n"
        )

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
    return prefix, focus_line, instruction

def _build_independent_analysis_prompt(
    existing_result: str,
    concern: str,
    context_files: list[str] | None,
    original_task: str,
) -> str:
    """
    构造independent_analysis完整prompt
    当context_files非空时，prefix从禁止读文件改为引导阅读+允许自主扩展
    为空时保持_MATERIAL_NOTICE
    """
    prefix = _MATERIAL_NOTICE
    if context_files:
        joined = "\n".join(f"- {p}" for p in context_files)
        prefix = (
            "[上下文文件]以下文件与本次分析相关，请先阅读它们，"
            "理解原始结论所涉及的代码实现，再开始批判性审视：\n"
            f"{joined}\n\n"
        )

    concern_block = ""
    if concern:
        concern_block = f"\n主Agent的具体关注点：{concern}\n请针对此关注点重点审查。"

    return (
        f"{prefix}"
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

def _build_validate_approach_prompt(
    approach: str,
    goal: str,
    context_files: list[str] | None,
) -> str:
    """
    构造validate_approach完整prompt
    当context_files非空时，增加引导阅读上下文文件的段落
    """
    ctx_block = ""
    if context_files:
        joined = "\n".join(f"- {p}" for p in context_files)
        ctx_block = (
            "[上下文文件]以下文件与本次验证相关，请先阅读它们理解现有实现和约束：\n"
            f"{joined}\n\n"
        )

    goal_block = ""
    if goal:
        goal_block = f"\n方案要达成的目标：{goal}\n请评估方案是否能达成此目标。"

    return (
        f"{ctx_block}"
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

def _build_consensus_prompt(
    question: str,
    context_files: list[str] | None,
) -> str:
    """ 构造consensus完整prompt """
    prompt = question
    if context_files:
        joined = "\n".join(f"- {p}" for p in context_files)
        prompt = (
            f"{question}\n\n"
            f"请优先阅读以下相关文件，无需扫描整个项目：\n{joined}"
        )
    prompt += (
        "\n\n请按以下结构输出你的独立判断：\n"
        "1.【问题复述】用自己的话复述问题，确认理解无误\n"
        "2.【核心观点】你对问题的直接回答（是/否/视情况而定，并说明理由）\n"
        "3.【关键论据】支撑你观点的主要依据（按重要性排序）\n"
        "4.【不确定之处】你不确定或缺乏足够信息判断的方面\n"
        "5.【替代视角】你可能忽略了的其他合理观点"
    )
    return prompt

def _build_test_audit_prompt(
    source_files: list[str],
    test_files: list[str] | None,
    focus: str,
) -> tuple[str, str]:
    """
    构造test_audit完整prompt，返回 (coverage_intro, focus_line)
    文件列表由handler直接拼入，避免重复join
    """
    source_block = "\n".join(f"- {p}" for p in source_files)
    if test_files:
        test_block = "\n".join(f"- {p}" for p in test_files)
        coverage_intro = (
            "请对比下方源码与现有测试，找出源码中尚未被测试覆盖的逻辑分支、边界条件与异常路径。\n\n"
            f"被测源码：\n{source_block}\n\n现有测试：\n{test_block}\n\n"
        )
    else:
        coverage_intro = (
            "下方为被测源码，目前没有提供现有测试文件。"
            "请基于源码逻辑给出一份应当覆盖的测试用例清单。\n\n"
            f"被测源码：\n{source_block}\n\n"
        )
    focus_line = f"本次审计重点：{focus}\n\n" if focus else ""
    return coverage_intro, focus_line

def _build_test_audit_full_prompt(
    source_files: list[str],
    test_files: list[str] | None,
    focus: str,
) -> str:
    """
    构造test_audit完整prompt（角色+审计流程+输出结构+只读提示）
    handler与_call_tool_by_name共用，保证单工具调用与链内调用产出一致
    """
    coverage_intro, focus_line = _build_test_audit_prompt(source_files, test_files, focus)
    return (
        "你是一名严谨的测试工程师。请阅读给定的文件，审计测试覆盖情况。\n\n"
        "审计流程：\n"
        "1. 先梳理源码的核心逻辑路径和关键分支，确保你理解了代码要做什么\n"
        "2. 对照现有测试（如有），逐条逻辑路径检查是否有测试覆盖\n"
        "3. 对未覆盖的路径，评估其风险等级并给出具体测试用例\n\n"
        f"{coverage_intro}{focus_line}"
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

def _build_advisor_draft_prompt(
    task: str,
    context_files: list[str] | None,
) -> str:
    """ 构造advisor_analysis草稿阶段prompt """
    draft_prompt = (
        f"{task}\n\n"
        "请给出你的结论。如果你对答案有把握，直接给结论即可；"
        "如果你不确定、或问题涉及高风险决策（架构选型/安全/数据完整性等），"
        f"在回答最末尾另起一行写出标记：{_NEED_ADVISOR_TAG}"
    )
    if context_files:
        joined = "\n".join(f"- {p}" for p in context_files)
        draft_prompt += f"\n\n请优先阅读以下相关文件，无需扫描整个项目：\n{joined}"
    return draft_prompt
