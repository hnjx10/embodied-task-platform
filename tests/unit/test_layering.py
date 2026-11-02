"""分层架构的强制约束。

架构文档里写「依赖单向向下」，如果没有机器校验，半年后一定会有人为了方便
在 ``skills`` 里 ``import robot_agent.api``。这个测试把口头约定变成 CI 门禁：

* 每一层只允许依赖**同层或更低层**（``LAYER_RANK`` 数字小的更底层）；
* 领域层不得依赖任何其他业务包——它被所有层共享，一旦它反过来依赖谁就成环；
* 组合根 ``factory`` 只允许交付层（``api`` / ``cli``）引用，业务包不得反向拿它；
* 同一层里互不相关的两个纵切（规划 / 安全、技能 / 存储）不得互相依赖。

检查基于 AST，只读源码，不导入模块——所以即使某个包在无 ROS 环境下无法导入，
这条规则依然生效。
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
PKG_ROOT = SRC / "robot_agent"

#: 层号越小越底层。同层之间允许协作，跨层只允许「上层 -> 下层」。
LAYER_RANK: dict[str, int] = {
    # L0 基础设施：谁都可以用，它们之间只允许 errors/version 这类无依赖模块
    "robot_agent.errors": 0,
    "robot_agent.version": 0,
    "robot_agent.logging": 0,
    "robot_agent.config": 0,
    "robot_agent.metrics": 0,
    "robot_agent.audit": 0,
    # L1 领域模型与纯函数（无外部依赖）
    "robot_agent.domain": 1,
    "robot_agent.reporting": 1,
    # L2 能力层：干活的东西（执行器 / 持久化）
    "robot_agent.skills": 2,
    "robot_agent.storage": 2,
    # L3 领域服务：把能力编排成决策（规划 / 安全）
    "robot_agent.planner": 3,
    "robot_agent.safety": 3,
    # L4 编排层：一次任务的生命周期
    "robot_agent.orchestration": 4,
    # L4 评估层：消费规划与安全，产出「这套防线到底拦不拦得住」的结论。
    # 与 orchestration 同为 L4 但互不依赖——评估只复用领域服务，不碰任务状态机。
    "robot_agent.evaluation": 4,
    # L5 组合根：唯一知道所有实现的地方
    "robot_agent.factory": 5,
    # L6 交付层：CLI 与 HTTP
    "robot_agent.api": 6,
    "robot_agent.cli": 6,
    "robot_agent.console": 6,  # 终端输出渲染，只被 cli 使用，不参与业务
}

#: 同层内互不依赖的纵切：允许依赖公共基础设施，但不允许彼此耦合。
INDEPENDENT_PAIRS = [
    ("robot_agent.planner", "robot_agent.safety"),
    ("robot_agent.skills", "robot_agent.storage"),
]

#: 允许引用组合根的包（其余包必须通过注入拿到依赖）。
COMPOSITION_ROOT = "robot_agent.factory"
COMPOSITION_ROOT_USERS = {"robot_agent.api", "robot_agent.cli", COMPOSITION_ROOT}


def _module_key(dotted: str) -> str:
    """把模块路径归并到「层」这一粒度：``robot_agent.skills.mock`` -> ``robot_agent.skills``。"""
    parts = dotted.split(".")
    if len(parts) <= 1:
        return "robot_agent"
    return ".".join(parts[:2])


def _module_files() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for path in sorted(PKG_ROOT.rglob("*.py")):
        rel = path.relative_to(SRC).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        out[".".join(parts)] = path
    return out


def _imported_modules(path: Path, module: str) -> set[str]:
    """解析单个模块里的 ``robot_agent.*`` 导入（含相对导入与 TYPE_CHECKING 块）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = module.rsplit(".", 1)[0] if "." in module else module
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if target.startswith("robot_agent"):
                found.add(target)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("robot_agent"):
                    found.add(alias.name)
    return found


@pytest.fixture(scope="module")
def edges() -> dict[str, set[str]]:
    """包级依赖边：``src_pkg -> {dst_pkg, ...}``（不含自引用）。"""
    graph: dict[str, set[str]] = collections.defaultdict(set)
    for module, path in _module_files().items():
        src = _module_key(module)
        if src == "robot_agent":
            continue  # 包根只做公开导出，不构成一层
        for target in _imported_modules(path, module):
            dst = _module_key(target)
            if dst != src and dst != "robot_agent":
                graph[src].add(dst)
    return dict(graph)


def test_every_package_declares_its_layer() -> None:
    """新增包必须登记层号，否则分层规则会被悄悄绕过。"""
    packages = {_module_key(m) for m in _module_files()} - {"robot_agent"}
    undeclared = sorted(packages - set(LAYER_RANK))
    assert not undeclared, f"以下包未在 LAYER_RANK 中声明所属层：{undeclared}"


def test_dependencies_point_downwards(edges: dict[str, set[str]]) -> None:
    """依赖只能指向同层或更低层。"""
    violations: list[str] = []
    for src, targets in sorted(edges.items()):
        for dst in sorted(targets):
            if LAYER_RANK[src] < LAYER_RANK[dst]:
                violations.append(f"{src} (L{LAYER_RANK[src]}) 反向依赖 {dst} (L{LAYER_RANK[dst]})")
    assert not violations, "分层被打破：\n  " + "\n  ".join(violations)


def test_domain_layer_is_pure(edges: dict[str, set[str]]) -> None:
    """领域层只做数据契约：不依赖任何其他业务包，也不碰配置/日志/存储。"""
    outward = sorted(edges.get("robot_agent.domain", set()))
    assert not outward, f"领域层出现了对外依赖：{outward}"


def test_independent_verticals_do_not_couple(edges: dict[str, set[str]]) -> None:
    """规划与安全、技能与存储是四个独立纵切，互相 import 说明职责划分错了。"""
    problems: list[str] = []
    for left, right in INDEPENDENT_PAIRS:
        if right in edges.get(left, set()):
            problems.append(f"{left} -> {right}")
        if left in edges.get(right, set()):
            problems.append(f"{right} -> {left}")
    assert not problems, "同层纵切出现耦合：\n  " + "\n  ".join(problems)


def test_only_delivery_touches_composition_root(edges: dict[str, set[str]]) -> None:
    """除交付层外，任何包都不该 import 组合根——否则依赖注入就白做了。"""
    offenders = sorted(
        src
        for src, targets in edges.items()
        if COMPOSITION_ROOT in targets and src not in COMPOSITION_ROOT_USERS
    )
    assert not offenders, f"以下包绕过了依赖注入直接拿组合根：{offenders}"


def test_package_graph_is_acyclic(edges: dict[str, set[str]]) -> None:
    """包之间有环意味着无法单独理解、单独测试任何一层。"""
    state: dict[str, int] = {}
    cycle: list[str] = []

    def visit(node: str, stack: list[str]) -> bool:
        state[node] = 1
        for nxt in sorted(edges.get(node, set())):
            if state.get(nxt) == 1:
                cycle.extend([*stack, node, nxt])
                return True
            if state.get(nxt, 0) == 0 and visit(nxt, [*stack, node]):
                return True
        state[node] = 2
        return False

    for node in sorted(edges):
        if state.get(node, 0) == 0 and visit(node, []):
            break
    assert not cycle, "包级依赖存在环：" + " -> ".join(cycle)
