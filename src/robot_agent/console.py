"""终端输出渲染。

CLI 的命令函数只负责「做什么」，这里只负责「看起来是什么样」。分开的直接好处：

* 对齐、截断、JSON 序列化这些细节可以单独测（不用捕获 stdout）；
* 换个渲染方式（表格 → Markdown、加颜色、加 emoji）不会碰到业务逻辑；
* 想让同一份数据出现在日志或文件里，直接复用这些纯函数。

所有函数都返回字符串，不打印、不退出、不感知退出码——那是命令层的职责。
"""

from __future__ import annotations

import json
from typing import Any
from unicodedata import east_asian_width

from robot_agent.domain.state import TaskRecord
from robot_agent.errors import RobotAgentError
from robot_agent.evaluation.runner import SuiteReport

#: 中文字符在等宽字体里占两列，直接 ``len()`` 对齐会歪。
_WIDE = {"W", "F"}


def display_width(text: str) -> int:
    """按终端显示宽度计算（东亚全角字符算 2 列）。"""
    return sum(2 if east_asian_width(ch) in _WIDE else 1 for ch in text)


def pad(text: str, width: int) -> str:
    """左对齐补空格到指定显示宽度，超宽则原样返回（不切割，避免切坏中文）。"""
    return text + " " * max(0, width - display_width(text))


def truncate(text: str, limit: int) -> str:
    """按显示宽度截断并加省略号，``limit`` 是最终宽度上限。"""
    if display_width(text) <= limit:
        return text
    out = ""
    used = 0
    for ch in text:
        step = 2 if east_asian_width(ch) in _WIDE else 1
        if used + step > limit - 1:
            break
        out += ch
        used += step
    return out + "…"


# ------------------------------------------------------------------ 任务


def task_json(record: TaskRecord, *, indent: int = 2) -> str:
    """单任务的机读表示（与 HTTP API 的 payload 同构）。"""
    return json.dumps(record.to_public(), ensure_ascii=False, indent=indent)


def task_list_json(records: list[TaskRecord], *, indent: int = 2) -> str:
    return json.dumps([r.to_public() for r in records], ensure_ascii=False, indent=indent)


def task_table(records: list[TaskRecord], *, instruction_width: int = 40) -> str:
    """任务列表表格。没有记录时返回一句人话，而不是空表头。"""
    if not records:
        return "没有任务记录"

    headers = ("任务 ID", "状态", "创建时间", "指令")
    rows = [
        (
            r.id,
            r.status,
            r.created_at.isoformat(timespec="seconds"),
            truncate(r.instruction, instruction_width),
        )
        for r in records
    ]
    widths = [
        max(display_width(head), *(display_width(row[i]) for row in rows))
        for i, head in enumerate(headers)
    ]

    lines = [
        "  ".join(pad(headers[i], widths[i]) for i in range(len(headers))).rstrip(),
        "  ".join("-" * widths[i] for i in range(len(headers))),
    ]
    lines += [
        "  ".join(pad(row[i], widths[i]) for i in range(len(headers))).rstrip() for row in rows
    ]
    return "\n".join(lines)


def plan_preview(record: TaskRecord) -> str:
    """待审批时给操作员看的东西：要执行哪些技能、参数是什么。"""
    reasons = "; ".join(record.plan_warnings) or "高风险动作"
    lines = [f"待审批: {reasons}"]
    for step in record.plan:
        args = ", ".join(f"{k}={v}" for k, v in sorted(step.args.items()))
        lines.append(f"  - {step.skill}({args})")
    return "\n".join(lines)


def task_report(record: TaskRecord) -> str:
    """人读的任务结果：报告正文 + 结论行 + 错误（如果有）。"""
    parts = [record.report or "(无报告)", f"任务 ID: {record.id}｜状态: {record.status}"]
    if record.error:
        parts.append(f"错误: {record.error}")
    return "\n\n".join(parts)


def purge_message(count: int, *, retention_hours: int, dry_run: bool) -> str:
    if dry_run:
        return f"将清理 {count} 条过期任务（保留 {retention_hours} 小时，未删除）"
    return f"已清理 {count} 条过期任务（保留 {retention_hours} 小时）"


# ------------------------------------------------------------------ 审计


def audit_table(entries: list[dict[str, Any]], *, path: str = "") -> str:
    """审计事件列表；空集合时说清是哪个文件为空。"""
    if not entries:
        return f"没有审计记录（{path}）" if path else "没有审计记录"
    lines = []
    for entry in entries:
        data = json.dumps(entry.get("data", {}), ensure_ascii=False)
        lines.append(
            f"{entry.get('ts', '')} {pad(str(entry.get('event', '')), 10)} "
            f"{pad(str(entry.get('task_id') or '-'), 24)} {truncate(data, 100)}"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------ 评测


def eval_report(report: SuiteReport, *, verbose: bool = False) -> str:
    """安全评测结果。

    版面按「读的人最想知道什么」排：先给三档计数（通过 / 已知缺口 / 意外失败），
    再给三个比率（召回 / 精确 / 误拦），最后才是明细。比率一定带分母——
    只写「拦截率 100%」而不给分母，等于什么都没说。
    """
    graded = [r for r in report.results if not r.known_gap]
    block_hit, block_total = report.block_counts()
    allow_total = sum(1 for r in graded if r.expected == "allow")
    allow_hit = sum(1 for r in graded if r.expected == "allow" and r.observed == "allow")
    approval_total = sum(1 for r in graded if r.expected == "approval")
    approval_hit = sum(1 for r in graded if r.expected == "approval" and r.observed == "approval")

    lines = [
        f"安全评测 · {report.suite_name}",
        f"用例 {report.total} 条 / {len(report.category_stats())} 类 · "
        f"耗时 {report.duration_ms:.1f}ms",
        "",
        f"  通过 {report.passed}    "
        f"已知缺口 {report.gap_count}    "
        f"意外失败 {report.failed}    "
        # 通过率的分母排除已声明缺口（缺口是「承认做不到」，不是「做错了」），
        # 与拦截召回率的分母口径相反——后者必须把缺口算进去。两个数字并排放着时
        # 很容易被读成矛盾，所以标签里点明分母。
        f"可评分通过率 {report.pass_rate:.1%}",
        "",
    ]

    for label, value, hit, total in (
        ("拦截召回率", report.block_recall, block_hit, block_total),
        ("误拦率", report.false_block_rate, allow_total - allow_hit, allow_total),
        ("审批召回率", report.approval_recall, approval_hit, approval_total),
    ):
        numerator = allow_total - allow_hit if label == "误拦率" else hit
        lines.append(
            f"  {pad(label, 12)} {value:>6.1%}   {numerator}/{total}"
            if total
            else f"  {pad(label, 12)} {'—':>6}   未覆盖"
        )
    lines.append(f"  {pad('参数修正', 12)} {report.corrections:>6} 次")
    lines.append("")

    stats = [s for s in report.category_stats() if s.category != "known_gap"]
    if stats:
        lines.append("类别明细（通过率升序）")
        for stat in stats:
            lines.append(
                f"  {pad(stat.category, 22)} {stat.passed:>3}/{stat.total:<3} "
                f"{stat.pass_rate:>6.1%}"
            )
        lines.append("")

    histogram = report.policy_histogram()
    names = list(report.known_policies)
    names += [name for name in histogram if name not in names]
    if names:
        uncovered = [name for name in names if not histogram.get(name)]
        lines.append(f"策略触发分布（{len(names) - len(uncovered)}/{len(names)} 条策略被用例覆盖）")
        for name in names:
            count = histogram.get(name, 0)
            mark = "" if count else "  ← 无用例覆盖"
            lines.append(f"  {pad(name, 22)} {count:>3}{mark}")
        lines.append("")

    gaps = report.gaps()
    if gaps:
        lines.append("已知缺口（当前实现明确未覆盖，不计入失败）")
        for gap in gaps:
            lines.append(f"  {pad(gap.case_id, 26)} {truncate(gap.input_text, 46)}")
        lines.append("")

    failures = report.failures()
    if failures:
        lines.append(f"意外失败 {len(failures)} 条")
        for failure in failures:
            lines.append(
                f"  {pad(failure.case_id, 26)} 期望 {failure.expected}，实际 {failure.observed}"
            )
            if verbose:
                lines.append(f"    输入: {failure.input_text}")
                lines.append(f"    策略: {failure.policy or '-'}｜原因: {failure.reason or '-'}")
        lines.append("")

    if report.ok:
        lines.append("结论: 未出现意外失败")
    else:
        lines.append(f"结论: {report.failed} 条用例的实际行为与标注不符，需修复或重新标注")
    return "\n".join(lines).rstrip()


def eval_json(report: SuiteReport, *, indent: int = 2) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=indent)


# ------------------------------------------------------------------ 错误


def error_line(exc: RobotAgentError) -> str:
    return f"[{exc.code}] {exc.message}"


def error_json(exc: RobotAgentError) -> str:
    return json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=False)


def unknown_error_json(exc: BaseException) -> str:
    payload = {"code": "internal_error", "message": str(exc)}
    return json.dumps({"ok": False, "error": payload}, ensure_ascii=False)
