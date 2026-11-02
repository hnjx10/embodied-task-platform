"""指标层测试：装了 prometheus-client 走真实实现，没装必须退化成可用 no-op。

「没装依赖就 import 失败」是这里最该防的回归——CI 的 `ros-import` job 只装 `.[dev]`，
曾经因为 no-op 桩不吞构造参数导致 `Counter(...)` 直接 TypeError，把整个 mock 模式拖死。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

from robot_agent import metrics


def test_prometheus_backend_available() -> None:
    # 开发环境装了 api extra，应当走真实实现
    assert metrics.PROMETHEUS_AVAILABLE is True


def test_metrics_renderable() -> None:
    metrics.TASKS_TOTAL.labels(status="succeeded").inc()
    payload, content_type = metrics.render_metrics()
    assert isinstance(payload, bytes)
    assert "text/plain" in content_type
    assert b"robot_agent_tasks_total" in payload


def test_noop_fallback_without_prometheus() -> None:
    """在子进程里屏蔽 prometheus_client，确认模块级指标仍可构造与调用。

    放子进程是因为 reload 会向默认 REGISTRY 重复注册同名指标而报错。
    """
    script = textwrap.dedent(
        """
        import sys
        sys.modules["prometheus_client"] = None  # 让 import 直接抛 ImportError

        import robot_agent.metrics as m

        assert m.PROMETHEUS_AVAILABLE is False, "屏蔽后不应认为后端可用"
        # 模块级构造（Counter/Histogram/Gauge 带实参）必须能跑通
        m.TASKS_TOTAL.labels(status="x").inc()
        m.TASK_DURATION.observe(1.0)
        m.TASKS_IN_FLIGHT.set(1)
        m.STEP_DURATION.labels(skill="goto").observe(0.1)
        m.AUDIT_WRITE_ERRORS_TOTAL.inc()

        payload, content_type = m.render_metrics()
        assert payload == b"", "no-op 后端不应产出指标文本"
        assert "text/plain" in content_type
        m.reset_for_tests()
        print("ok")
        """
    )
    result = subprocess.run(  # noqa: S603 - 固定可执行文件与固定脚本，无外部输入
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
