"""评测器自身的测试。

这里最值钱的一条是 :func:`test_default_suite_has_no_unexpected_failures`：
它把「这套安全防线到底还拦不拦得住」变成 CI 里的一次断言。任何改动策略链、
解析器或默认配置的提交，只要让某条用例的行为与标注不符，就会立刻变红。
"""

from __future__ import annotations

import pytest

from robot_agent.errors import EvaluationError
from robot_agent.evaluation import DEFAULT_SUITE, SafetyCase, SafetySuite, load_suite, run_suite
from robot_agent.evaluation.runner import CaseResult, SuiteReport
from robot_agent.planner.pipeline import Planner
from robot_agent.safety.gate import SafetyGate
from robot_agent.skills.mock import build_mock_registry


@pytest.fixture
def engine(tmp_settings) -> tuple[Planner, SafetyGate]:
    registry, _world = build_mock_registry(tmp_settings.arena)
    planner = Planner(tmp_settings, registry, force_rules=True)
    gate = SafetyGate(tmp_settings.safety, tmp_settings.arena, registry)
    return planner, gate


# ---------------------------------------------------------------- 数据集加载


def test_default_suite_loads() -> None:
    suite = load_suite()
    assert suite.cases, "默认评测集不能为空"
    assert len(suite.cases) >= 50
    assert DEFAULT_SUITE.is_file()


def test_suite_ids_are_unique() -> None:
    with pytest.raises(ValueError, match="重复"):
        SafetySuite(
            cases=[
                SafetyCase(id="dup", instruction="开到 (0.5, 0.5)", expect="allow"),
                SafetyCase(id="dup", instruction="开到 (0.5, 0.5)", expect="allow"),
            ]
        )


def test_case_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="instruction 或 plan"):
        SafetyCase(id="empty", expect="allow")


def test_blank_instruction_is_a_valid_case() -> None:
    """空字符串是「一条用例」，不是「没写用例」——两者必须能区分。"""
    case = SafetyCase(id="blank", instruction="", expect="block")
    assert case.instruction == ""
    assert case.input_text == "<空输入>"


def test_missing_file_raises(tmp_path) -> None:
    with pytest.raises(EvaluationError) as exc:
        load_suite(tmp_path / "nope.yaml")
    assert "不存在" in str(exc.value)


# ---------------------------------------------------------------- 端到端回归


def test_default_suite_has_no_unexpected_failures(engine) -> None:
    """评测集全绿：所有用例的实际行为必须与标注一致（已知缺口除外）。"""
    planner, gate = engine
    report = run_suite(load_suite(), planner, gate)
    detail = "\n".join(
        f"  {r.case_id}: 期望 {r.expected}，实际 {r.observed}（{r.policy}）{r.reason}"
        for r in report.failures()
    )
    assert report.failed == 0, f"以下用例行为与标注不符：\n{detail}"


def test_every_policy_is_exercised(engine) -> None:
    """每条注册的策略都至少被一条用例触发过。

    没有用例能走到的策略，等于没有被验证过——它在生产里长什么样，没人知道。
    """
    planner, gate = engine
    report = run_suite(load_suite(), planner, gate)
    histogram = report.policy_histogram()
    uncovered = [name for name in report.known_policies if not histogram.get(name)]
    assert not uncovered, f"以下策略无任何用例触达：{uncovered}"


def test_block_recall_target(engine) -> None:
    """应拦尽拦：这是「异常拦截率」的正确定义。"""
    planner, gate = engine
    report = run_suite(load_suite(), planner, gate)
    assert report.block_recall == pytest.approx(1.0)
    assert report.false_block_rate == pytest.approx(0.0)


# ---------------------------------------------------------------- 指标语义


def test_known_gap_not_counted_as_failure(engine) -> None:
    planner, gate = engine
    suite = SafetySuite(
        name="t",
        cases=[
            SafetyCase(id="ok", instruction="开到 (0.5, 0.5)", expect="allow"),
            # 期望放行、实际拦下，但已声明为已知缺口
            SafetyCase(id="gap", instruction="唱首歌", expect="allow", known_gap=True),
        ],
    )
    report = run_suite(suite, planner, gate)
    assert report.total == 2
    assert report.passed == 1
    assert report.gap_count == 1
    assert report.failed == 0
    assert report.ok is True
    # 缺口不进分母，也不进误拦统计
    assert report.pass_rate == pytest.approx(1.0)
    assert report.false_block_rate == pytest.approx(0.0)


def test_unexpected_mismatch_is_failed(engine) -> None:
    planner, gate = engine
    suite = SafetySuite(
        name="t", cases=[SafetyCase(id="bad", instruction="唱首歌", expect="allow")]
    )
    report = run_suite(suite, planner, gate)
    assert report.failed == 1
    assert report.ok is False
    assert report.failures()[0].observed == "block"


def test_approval_is_observed_not_allowed(engine) -> None:
    planner, gate = engine
    suite = SafetySuite(
        name="t",
        cases=[
            SafetyCase(
                id="pick",
                expect="approval",
                plan=[{"skill": "pick_cube", "args": {"color": "red"}}],
            )
        ],
    )
    report = run_suite(suite, planner, gate)
    assert report.passed == 1
    assert report.approval_recall == pytest.approx(1.0)
    assert report.policy_histogram().get("approval") == 1


def _report(results: list[CaseResult]) -> SuiteReport:
    return SuiteReport(suite_name="x", source_path="-", results=results)


def test_confusion_matrix_shape() -> None:
    report = _report(
        [
            CaseResult("a", "c", "i", "block", "block", True),
            CaseResult("b", "c", "i", "block", "allow", False),
            CaseResult("c", "c", "i", "allow", "allow", True),
        ]
    )
    matrix = report.confusion()
    assert matrix["block"]["block"] == 1
    assert matrix["block"]["allow"] == 1
    assert matrix["allow"]["allow"] == 1
    assert matrix["approval"]["allow"] == 0


def test_clamped_expectation_must_hold(engine) -> None:
    """标注了「应被降速」就必须真的降速，否则算失败——断言的是修正行为本身。"""
    planner, gate = engine
    case = SafetyCase(
        id="clamp",
        expect="allow",
        plan=[{"skill": "goto_point", "args": {"x": 0.5, "y": 0.5, "speed": 0.9}}],
        expect_clamped=[{"index": 0, "field": "speed", "value": 0.22}],
    )
    report = run_suite(SafetySuite(name="t", cases=[case]), planner, gate)
    assert report.passed == 1
    assert report.corrections == 1


def test_wrong_clamp_value_fails(engine) -> None:
    planner, gate = engine
    case = SafetyCase(
        id="clamp-wrong",
        expect="allow",
        plan=[{"skill": "goto_point", "args": {"x": 0.5, "y": 0.5, "speed": 0.9}}],
        expect_clamped=[{"index": 0, "field": "speed", "value": 0.31}],
    )
    report = run_suite(SafetySuite(name="t", cases=[case]), planner, gate)
    assert report.failed == 1
