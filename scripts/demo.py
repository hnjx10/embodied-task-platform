#!/usr/bin/env python3
"""CLI 入口：python scripts/demo.py --mock "把红色方块推到左边" """
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from audit import AuditLog  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Gazebo + LangGraph 任务编排 demo")
    parser.add_argument("instruction", help="自然语言指令")
    parser.add_argument("--mock", action="store_true", help="使用 mock 执行器（无需 ROS）")
    parser.add_argument("--ros", action="store_true", help="使用 ROS2 执行器")
    parser.add_argument("--approve", action="store_true", help="执行前人工挂起确认")
    args = parser.parse_args()

    from graph import build_graph

    if args.ros:
        from skills.ros_skills import build_ros_registry
        registry, handle = build_ros_registry()
    else:
        from skills.mock_skills import build_mock_registry
        registry, handle = build_mock_registry()

    audit = AuditLog(os.path.join(os.path.dirname(__file__), "..", "logs", "audit.jsonl"))
    app = build_graph(registry, audit, need_approval=args.approve)

    config = {"configurable": {"thread_id": "demo-1"}} if args.approve else None
    state = app.invoke({"instruction": args.instruction, "steps": [], "idx": 0,
                        "current": {}, "results": [], "blocked": False, "report": ""},
                       config=config)
    if args.approve and len(state["results"]) == 0:
        print("\n[挂起] 即将执行技能序列，确认请再次运行: "
              "app.invoke(None, config)  或加 --resume\n")
        return 0
    if args.ros:
        handle.shutdown()
    print("\n" + state["report"])
    return 0 if state["results"] and all(r["ok"] for r in state["results"]) else 1


if __name__ == "__main__":
    sys.exit(main())
