"""安全评测：把「拦得住」从一句结论变成一条命令。

对外只有三个入口：加载数据集、跑一遍、看报告。命令行封装在 ``robot-agent eval``。

    suite = load_suite()
    report = run_suite(suite, planner, gate)
    print(report.metrics()["block_recall"])
"""

from robot_agent.evaluation.cases import DEFAULT_SUITE, SafetyCase, SafetySuite, load_suite
from robot_agent.evaluation.runner import CaseResult, SuiteReport, evaluate_case, run_suite

__all__ = [
    "DEFAULT_SUITE",
    "CaseResult",
    "SafetyCase",
    "SafetySuite",
    "SuiteReport",
    "evaluate_case",
    "load_suite",
    "run_suite",
]
