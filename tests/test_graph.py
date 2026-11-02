"""mock 模式端到端回归：正常任务、越界拦截、超速拦截、schema 失败。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from audit import AuditLog
from graph import build_graph
from skills.mock_skills import build_mock_registry


def make_app(tmp_path):
    registry, world = build_mock_registry()
    audit = AuditLog(str(tmp_path / "audit.jsonl"))
    return build_graph(registry, audit), world


def _invoke(app, instruction):
    return app.invoke({"instruction": instruction, "steps": [], "idx": 0,
                       "current": {}, "results": [], "blocked": False, "report": ""})


def test_push_cube_to_left(tmp_path):
    app, world = make_app(tmp_path)
    state = _invoke(app, "把红色方块推到左边")
    assert world.cubes["red"] == (-1.2, 0.0)
    assert all(r["ok"] for r in state["results"])
    assert "任务完成" in state["report"]


def test_out_of_arena_blocked(tmp_path):
    app, _ = make_app(tmp_path)
    state = _invoke(app, "把绿色方块推到 (5.0, 5.0)")
    assert not state["results"][0]["ok"]
    assert "超出场地边界" in state["results"][0]["detail"]
    assert "任务未完成" in state["report"]


def test_overspeed_blocked(tmp_path):
    app, _ = make_app(tmp_path)
    state = _invoke(app, "全速开到 (1.0, 1.0)")
    # 规则兜底解析出 speed=0.5，超过 0.22 上限 → 拦截
    assert not state["results"][0]["ok"]
    assert "超过硬件上限" in state["results"][0]["detail"]


def test_geofence_blocked(tmp_path):
    app, _ = make_app(tmp_path)
    state = _invoke(app, "把蓝色方块推到 (1.3, 1.3)")
    assert "禁区" in state["results"][0]["detail"]


def test_unknown_instruction_raises_in_plan(tmp_path):
    app, _ = make_app(tmp_path)
    # 规则兜底无法解析且无 LLM → plan 节点抛异常，invoke 直接失败（预期行为）
    try:
        _invoke(app, "给我唱首歌")
        raised = False
    except Exception:
        raised = True
    assert raised
