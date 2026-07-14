"""
prompts.py——AgentParliament的prompt构造与共享记忆体注入

职责：
1. 构造各工具的完整prompt（纯函数，供server.py的工具handler与chain.py的_call_tool_by_name复用，
   保证单工具调用与链内调用产出完全一致）
2. 读取副Agent工作目录下的CLAUDE.md/SPEC.md/docs/adr，渲染成注入prompt开头的记忆块
   （显式注入而非依赖Claude Code自动加载，确保headless模式下副Agent确定性地获得项目上下文）
3. delegate_dialogue专用prompt构造（首轮含对话能力指引、续答轮注入上轮结论+主Agent回答）
4. [ASK_PARENT]提问标记检测（detect_ask_parent）

本模块是纯叶子模块：不import任何项目内其他模块，不持有全局可变状态。
"""
from __future__ import annotations

from pathlib import Path
import re

# 副Agent以只读模式运行（plan或default+只读白名单），无法写文件。明确告知以抑制幻觉回复
# 文案不提及具体运行模式：peer_review仍用plan模式，迁移到default的工具共用此notice，二者都不能写
_READONLY_NOTICE = (
    "\n\n[重要提醒] 你只能读取文件，无法创建、修改或删除任何文件。"
    "不要在回复中声称已创建文件或已写入内容，直接输出你的分析文本即可。"
)

# 审查类工具（peer_review）的全部材料已随prompt给出，约束副Agent聚焦diff而非跑去读cwd文件
# 仅供设计意图就是"只看材料"的场景使用；分析类工具改用下方的_EXPLORATION_NOTICE
_MATERIAL_NOTICE = (
    "[约束] 全部待审查的材料已在下方完整给出，"
    "请直接基于下方内容工作，无需读取任何文件。\n\n"
)

# 分析类工具的探索型引导：材料已在下方给出，但允许并鼓励主动读代码核实
# 用于independent_analysis/validate_approach不传context_files时，替代原_MATERIAL_NOTICE的"禁止读文件"嘴套
_EXPLORATION_NOTICE = (
    "[说明] 主要材料已在下方给出，作为你分析的起点。"
    "如需核实实现细节、查找相关定义或验证结论是否属实，"
    "你可以主动读取相关代码文件——这能让你的分析更扎实。\n\n"
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
    无论是否传context_files，都允许并鼓励主动读代码核实结论真实性（Tier 1探索能力）：
    - 有context_files：给出推荐入口文件
    - 无context_files：用_EXPLORATION_NOTICE允许基于下方材料自由扩展探索
    两者都替代了旧的"禁止读文件"嘴套
    """
    prefix = _EXPLORATION_NOTICE
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
        f"{_NO_HALT_NOTICE}"
    )

def _build_validate_approach_prompt(
    approach: str,
    goal: str,
    context_files: list[str] | None,
) -> str:
    """
    构造validate_approach完整prompt
    无论是否传context_files，都允许主动读代码核实方案可行性（Tier 1探索能力）：
    - 有context_files：给出推荐入口文件
    - 无context_files：用_EXPLORATION_NOTICE允许自由探索现有实现
    """
    ctx_block = _EXPLORATION_NOTICE
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
        f"{_NO_HALT_NOTICE}"
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
        f"{_NO_HALT_NOTICE}"
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


# ─── delegate_dialogue 对话哨兵与续答 prompt ──────────────────────────
# 子Agent在调研中途遇到需主Agent确认的方向时，在结论末尾输出 [ASK_PARENT] 问题 标记结束本轮；
# 主进程检测标记、保存对话上下文（session），主Agent带 session_id+answer 再次调用续答。
# 复用 _NEED_ADVISOR_TAG 的哨兵模式，区别：_NEED_ADVISOR 是“结束后升级求助强模型”，
# _ASK_PARENT 是“中途向主Agent提问并继续同一任务”。

_ASK_PARENT_TAG = "[ASK_PARENT]"
# 匹配 [ASK_PARENT] 标记及之后的内容；question 取标记后第一行，多行内容归入结论
_ASK_PARENT_PATTERN = re.compile(r"\[ASK_PARENT\]\s*(.+)", re.DOTALL)

def detect_ask_parent(text: str) -> tuple[bool, str, str]:
    """
    从子Agent输出检测 [ASK_PARENT] 提问标记
    返回 (has_ask, question, conclusion_without_tag)：
    - has_ask=True：question 为标记后第一行（问题摘要），conclusion 为去标记后的原文
    - has_ask=False：question="", conclusion=text 原样
    best-effort：无标记返回原文，不阻断主流程
    """
    m = _ASK_PARENT_PATTERN.search(text)
    if not m:
        return False, "", text
    question = m.group(1).split("\n")[0].strip()
    conclusion = _ASK_PARENT_PATTERN.sub("", text).rstrip()
    return True, question, conclusion

def _build_dialogue_prompt(question: str, context_files: list[str] | None) -> str:
    """
    构造 delegate_dialogue 首轮 prompt：复用 _build_research_prompt，追加对话能力指引
    指引子Agent在遇需主Agent确认方向时，输出 [ASK_PARENT] 问题 结束本轮
    """
    prompt = _build_research_prompt(question, context_files)
    prompt += (
        "\n\n[对话能力] 若在调研中遇到需要主Agent确认的方向"
        "（如：多个可行路径不知选哪个、发现超出调研范围的关键风险、"
        "需要主Agent提供额外业务约束等），"
        f"请在结论末尾另起一行输出标记 `{_ASK_PARENT_TAG} 你的问题`，"
        "然后结束本轮。主Agent回答后会让你继续同一任务。"
        "若无此需求，正常输出结论即可，不要输出该标记。"
    )
    return prompt

def _build_dialogue_continue_prompt(
    prev_conclusion: str,
    answer: str,
    original_task: str,
) -> str:
    """
    构造 delegate_dialogue 续答轮 prompt：注入上轮结论 + 主Agent回答 + 继续指令
    prev_conclusion 已由调用方截断（_truncate_for_chain），此处不再截断
    """
    return (
        f"原始任务：{original_task}\n\n"
        f"你上一轮的结论如下（供你回忆上下文，不要重复输出已述内容）：\n{prev_conclusion}\n\n"
        f"主Agent对你提问的回答：{answer}\n\n"
        "请基于此回答继续完成原始任务，输出最终结论。"
        f"若仍有需主Agent确认的方向，可再次输出 {_ASK_PARENT_TAG} 问题。"
        f"{_NO_HALT_NOTICE}"
    )


# ─── verify_implementation（Tier 2 隔离可写可执行）──────────────────
# 这个工具的子 Agent 在 git worktree 隔离副本内工作，拥有 Write/Edit/Bash(测试命令) 权限。
# 目标：设计并执行测试，验证一个方案/实现是否真的可行，产出 ground truth（测试是否通过）。

# verify_implementation 专用 notice：告知子 Agent 它处于可写可执行的隔离环境，
# 与只读工具的 _READONLY_NOTICE 不同——这里明确允许写文件和执行命令
_SANDBOX_NOTICE = (
    "\n\n[环境说明] 你在一个隔离的工作副本（git worktree）中运行，"
    "拥有完整文件读写权限和命令执行权限。"
    "你可以新建测试文件、修改源码、运行测试命令（如 pytest/python）。"
    "你在工作副本内的所有改动都不会影响主仓库——它们会被提取为建议性 diff，交主 Agent 审批。"
)

def _build_verify_implementation_prompt(
    task: str,
    test_command: str,
    focus: str,
) -> str:
    """
    构造 verify_implementation 完整 prompt
    引导子 Agent：读懂要验证的代码 → 设计测试 → 写测试文件 → 运行测试 → 报告 ground truth
    test_command 为空时让子 Agent 自行决定如何运行测试
    """
    test_cmd_section = (
        f"请使用以下命令运行测试：\n```\n{test_command}\n```\n\n"
        if test_command
        else "请自行决定如何运行测试（根据项目使用的测试框架，如 pytest/unittest/go test 等）。\n\n"
    )

    focus_line = f"本次验证重点：{focus}\n\n" if focus else ""

    return (
        "你是一名严谨的测试工程师，在一个隔离的工作副本中工作。"
        "你的任务是设计并实际执行测试，验证一个实现/方案是否真的可行。\n\n"
        "工作流程：\n"
        "1. 先阅读相关源码，理解要验证的逻辑和它应有的行为\n"
        "2. 设计能覆盖核心逻辑、边界条件和异常路径的测试用例\n"
        "3. 在工作副本中创建测试文件（可新建，也可修改现有测试）\n"
        "4. 运行测试，捕获完整的测试输出（通过/失败/错误信息）\n"
        "5. 如果测试失败，分析失败原因：是代码有 bug，还是测试本身写错了\n"
        "6. 如有必要，修改源码修复发现的问题，重新运行测试确认修复\n\n"
        f"{test_cmd_section}{focus_line}"
        "请按以下结构输出你的最终结论：\n"
        "1.【验证结论】测试是否全部通过（通过/失败/部分通过），一句话总结\n"
        "2.【测试覆盖】你设计了哪些测试用例，覆盖了哪些场景\n"
        "3.【测试输出】运行测试的完整输出（关键的通过/失败行，不要省略失败详情）\n"
        "4.【发现问题】如果测试失败或发现 bug，详细描述：失败用例、期望值 vs 实际值、根因分析\n"
        "5.【改动说明】你在工作副本中做了哪些改动（新建/修改了哪些文件，为什么）\n"
        "6.【建议】对主 Agent 的建议：方案是否可行、是否需要调整、是否值得采纳你的改动\n\n"
        "重要：你的价值是产出 ground truth（测试真的跑过了），而不是纸上谈兵。"
        "即使测试失败，也是有价值的发现——失败本身就是在帮主 Agent 提前排雷。\n\n"
        f"要验证的任务：{task}"
        f"{_SANDBOX_NOTICE}"
        f"{_NO_HALT_NOTICE}"
    )

