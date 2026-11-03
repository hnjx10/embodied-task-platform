"""端到端时延基准：让「快了多少」变成一条能重跑的命令。

**为什么需要这个脚本**

性能数字是简历和技术面里最容易被追问、也最容易翻车的一类断言。写
「3s → 800ms」很容易，但被问「怎么测的、分母是什么、什么机器上」时答不上来，
前面所有的工程细节都会被连带怀疑。

所以这里的做法是：**先有可复现的测量，再谈数字**。脚本输出 P50 / P95 而不是
平均值——平均值会被长尾掩盖，而用户感知到的恰恰是长尾。

**口径必须写清楚**（README 里引用了这组数）：

* 规划：规则规划器（``force_rules=True``）。接真实 LLM 时这一段由网络与模型
  决定，本地测不出来，也不该混在一起报。
* 闸门：7 条策略串行，纯 CPU。
* 端到端：mock 执行器 + 真 SQLite + 真审计文件。不含 ROS2 与网络。
* 并发：N 个任务同时提交，对比逐个 ``await`` 的总耗时。

运行：

    python benchmarks/latency.py
    python benchmarks/latency.py --rounds 500 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from robot_agent.config import ArenaSettings, SafetySettings, Settings
from robot_agent.domain.plan import Plan
from robot_agent.factory import build_assembly
from robot_agent.logging import configure_logging
from robot_agent.planner.pipeline import Planner
from robot_agent.planner.rules import RulePlanner
from robot_agent.safety.gate import SafetyGate
from robot_agent.skills.mock import build_mock_registry

#: 一批有代表性的指令：覆盖单步、多步、相对位移、查询。
INSTRUCTIONS = (
    "把红色方块推到左边",
    "开到 (1.0, 1.0)",
    "往前开一米",
    "把绿色方块推到 (0.5, -0.5)",
    "机器人在哪",
)


def _percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, round(pct / 100.0 * (len(ordered) - 1))))
    return ordered[index]


def _summarize(samples_ms: list[float]) -> dict[str, float]:
    return {
        "n": len(samples_ms),
        "p50_ms": round(_percentile(samples_ms, 50), 3),
        "p95_ms": round(_percentile(samples_ms, 95), 3),
        "mean_ms": round(statistics.fmean(samples_ms), 3),
        "max_ms": round(max(samples_ms), 3),
    }


def bench_planning(rounds: int) -> dict[str, float]:
    """规划 + 参数校验，不含模型调用。"""
    planner = RulePlanner()
    samples: list[float] = []
    for i in range(rounds):
        instruction = INSTRUCTIONS[i % len(INSTRUCTIONS)]
        started = time.perf_counter()
        try:
            planner.plan(instruction)
        except Exception:
            # 无法解析的指令同样计入耗时：失败也要花时间，不能只统计成功路径
            pass
        samples.append((time.perf_counter() - started) * 1000.0)
    return _summarize(samples)


def bench_gate(rounds: int) -> dict[str, float]:
    """策略链本身的开销（7 条策略串行）。"""
    settings = Settings(env="test", safety=SafetySettings(), arena=ArenaSettings())
    registry, _ = build_mock_registry(settings.arena)
    planner = RulePlanner()
    gate = SafetyGate(settings.safety, settings.arena, registry)

    plans: list[tuple[Any, str]] = []
    for instruction in INSTRUCTIONS:
        try:
            # RulePlanner.plan 返回 list[PlanStep]，闸门要的是 Plan 契约对象
            plans.append((Plan(steps=planner.plan(instruction), source="rules"), instruction))
        except Exception:
            continue

    samples: list[float] = []
    for i in range(rounds):
        plan, instruction = plans[i % len(plans)]
        started = time.perf_counter()
        gate.screen(plan, instruction=instruction, task_id="bench")
        samples.append((time.perf_counter() - started) * 1000.0)
    return _summarize(samples)


def bench_plan_cache(rounds: int) -> dict[str, float]:
    """命中规划缓存的耗时——高频指令走的就是这条路径。

    只量「命中」这一段：未命中时的时间取决于网关，本机量不出别人的模型。
    命中路径的成本应该稳定在微秒级，一旦它开始随指令长度变化，说明缓存
    退化成了「每次都重新校验」。
    """
    settings = Settings(
        env="test",
        storage={"url": "memory"},
        plan_cache={"enabled": True, "max_entries": 256, "ttl_s": 300.0},
    )
    registry, _ = build_mock_registry(settings.arena)
    planner = Planner(settings, registry, force_rules=True)
    warm = INSTRUCTIONS[0]
    planner.plan(warm)  # 预热，保证后续都命中

    samples: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        planner.plan(warm)
        samples.append((time.perf_counter() - started) * 1000.0)
    return _summarize(samples)


def _bench_settings(tmp: Path) -> Settings:
    return Settings(
        env="test",
        log_level="CRITICAL",
        safety=SafetySettings(),
        arena=ArenaSettings(),
        executor={"backend": "mock", "step_timeout_s": 5.0, "max_attempts": 1},
        audit={"enabled": True, "path": tmp / "audit.jsonl"},
        storage={"url": f"sqlite:///{tmp / 'tasks.db'}"},
    )


async def bench_end_to_end(rounds: int) -> dict[str, float]:
    """完整链路：规划 → 闸门 → 执行 → 核验 → 报告 → 落库。"""
    with tempfile.TemporaryDirectory() as raw:
        assembly = await build_assembly(_bench_settings(Path(raw)), force_rules=True)
        try:
            samples: list[float] = []
            for i in range(rounds):
                task = await assembly.runner.submit(INSTRUCTIONS[i % len(INSTRUCTIONS)])
                started = time.perf_counter()
                await assembly.runner.execute(task.id)
                samples.append((time.perf_counter() - started) * 1000.0)
            return _summarize(samples)
        finally:
            await assembly.aclose()


async def bench_concurrency(batch: int) -> dict[str, float]:
    """并发提交 vs 逐个执行。

    这才是本项目里真实存在的「无依赖环节并行」：多个任务之间没有数据依赖，
    串行执行纯属浪费。物理动作仍由信号量串行化（那是物理约束），但规划、
    闸门、落库、核验这些阶段可以重叠。
    """
    with tempfile.TemporaryDirectory() as raw:
        assembly = await build_assembly(_bench_settings(Path(raw)), force_rules=True)
        try:
            prepared = [await assembly.runner.submit(INSTRUCTIONS[i % len(INSTRUCTIONS)])
                        for i in range(batch)]

            started = time.perf_counter()
            for task in prepared:
                await assembly.runner.execute(task.id)
            serial_ms = (time.perf_counter() - started) * 1000.0

            prepared = [await assembly.runner.submit(INSTRUCTIONS[i % len(INSTRUCTIONS)])
                        for i in range(batch)]
            started = time.perf_counter()
            await asyncio.gather(*(assembly.runner.execute(t.id) for t in prepared))
            parallel_ms = (time.perf_counter() - started) * 1000.0

            return {
                "batch": batch,
                "serial_ms": round(serial_ms, 1),
                "parallel_ms": round(parallel_ms, 1),
                "speedup": round(serial_ms / parallel_ms, 2) if parallel_ms else 0.0,
            }
        finally:
            await assembly.aclose()


def _print_table(title: str, rows: dict[str, dict[str, Any]]) -> None:
    print(f"\n{title}")
    print(f"  {'阶段':<14}{'n':>6}{'P50':>10}{'P95':>10}{'mean':>10}{'max':>10}")
    for name, stat in rows.items():
        print(
            f"  {name:<14}{stat['n']:>6}{stat['p50_ms']:>9.2f}ms"
            f"{stat['p95_ms']:>9.2f}ms{stat['mean_ms']:>9.2f}ms{stat['max_ms']:>9.2f}ms"
        )


async def main_async(rounds: int, batch: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "planning": bench_planning(rounds),
        "plan_cache": bench_plan_cache(rounds),
        "screen": bench_gate(rounds),
        "end_to_end": await bench_end_to_end(max(20, rounds // 10)),
        "concurrency": await bench_concurrency(batch),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="robot-agent 时延基准")
    parser.add_argument("--rounds", type=int, default=200, help="每个阶段的采样次数")
    parser.add_argument("--batch", type=int, default=16, help="并发对比的任务数")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    configure_logging("CRITICAL")
    result = asyncio.run(main_async(args.rounds, args.batch))

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    _print_table(
        "单阶段耗时（mock 执行器 / 规则规划器，不含网络与模型调用）",
        {k: result[k] for k in ("planning", "plan_cache", "screen", "end_to_end")},
    )
    print("  planning = 未命中（真算）    plan_cache = 命中缓存（查表）")
    concurrency = result["concurrency"]
    print("\n任务级并发（规划/闸门/落库重叠，物理动作仍由信号量串行）")
    print(
        f"  {concurrency['batch']} 个任务：串行 {concurrency['serial_ms']:.0f}ms"
        f" → 并发 {concurrency['parallel_ms']:.0f}ms"
        f"（{concurrency['speedup']:.2f}×）"
    )
    print()


if __name__ == "__main__":
    main()
