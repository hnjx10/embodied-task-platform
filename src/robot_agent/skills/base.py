"""技能抽象与注册表。

设计要点：

* **参数 schema 即类型**：每个技能的参数用 pydantic 模型声明，既生成给 LLM 的
  函数描述，也做运行时校验。LLM 幻觉出的多余参数会被 ``extra="forbid"`` 直接拒绝，
  而不是悄悄传进执行器。
* **执行入口唯一**：外部只能调 :meth:`Skill.execute`，由基类统一做参数校验、
  计时、异常归一化与指标埋点；子类只实现 :meth:`Skill.run`，无法绕过校验。
* **与执行器解耦**：ROS2 与 mock 实现同一组 ``name``，编排层依赖
  :class:`SkillRegistry` 而非具体实现（依赖倒置），CI 无仿真也能全链路回归。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from importlib.metadata import entry_points
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from robot_agent import metrics
from robot_agent.errors import SkillExecutionError, SkillNotFoundError
from robot_agent.logging import get_logger

logger = get_logger(__name__)

RiskLevel = Literal["low", "medium", "high"]


class SkillParams(BaseModel):
    """技能参数基类。默认拒绝未声明字段——LLM 多给参数即视为非法计划。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SkillResult(BaseModel):
    """技能执行结果。"""

    model_config = ConfigDict(extra="allow")

    ok: bool
    detail: str = ""
    data: dict[str, Any] = {}
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "data": self.data,
            "latency_ms": round(self.latency_ms, 3),
        }


class Skill(ABC):
    """机器人能力抽象。子类只需声明 ``name`` / ``description`` / ``params_model``。"""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    params_model: ClassVar[type[SkillParams]] = SkillParams
    risk: ClassVar[RiskLevel] = "low"
    # 该技能是否为「物理动作」——verify 阶段据此判断是否需要实测核验
    is_motion: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.name and not cls.description:
            raise TypeError(f"技能 {cls.__name__} 必须提供 description：它会作为 LLM 的函数描述")

    # ------------------------------------------------------------------ 对外

    def execute(self, **kwargs: Any) -> SkillResult:
        """唯一外部入口：校验 → 计时 → 执行 → 异常归一化。

        注意把**校验后的对象**传给 :meth:`run`，而不是原始 kwargs：否则声明为
        ``list[GotoParams]`` 的参数实际收到的是 ``list[dict]``，类型标注变成一句空话，
        嵌套模型在技能实现里全部退化成字典访问（这个问题在 ``patrol`` 上真实发生过）。
        """
        payload: dict[str, Any] = dict(self.validate_params(kwargs)) if kwargs else {}
        started = time.perf_counter()
        try:
            result = self.run(**payload)
        except SkillExecutionError:
            raise
        except Exception as exc:
            elapsed = time.perf_counter() - started
            metrics.STEP_DURATION.labels(skill=self.name).observe(elapsed)
            metrics.STEPS_TOTAL.labels(skill=self.name, outcome="error").inc()
            logger.error(
                "skill_failed", skill=self.name, args=kwargs, error=str(exc), exc_info=True
            )
            raise SkillExecutionError(
                f"技能 {self.name} 执行异常: {exc}", details={"skill": self.name}, cause=exc
            ) from exc

        elapsed = time.perf_counter() - started
        result.latency_ms = elapsed * 1000.0
        outcome = "ok" if result.ok else "rejected"
        metrics.STEPS_TOTAL.labels(skill=self.name, outcome=outcome).inc()
        metrics.STEP_DURATION.labels(skill=self.name).observe(elapsed)
        return result

    def validate_params(self, args: dict[str, Any]) -> SkillParams:
        """参数校验，失败转成带技能名的结构化错误。"""
        try:
            return self.params_model.model_validate(args)
        except ValidationError as exc:
            raise SkillExecutionError(
                f"技能 {self.name} 参数非法: {_compact_errors(exc)}",
                details={"skill": self.name, "args": args},
            ) from exc

    @abstractmethod
    def run(self, **kwargs: Any) -> SkillResult:
        """实际动作。子类实现。"""

    # ------------------------------------------------------------------ 内省

    @classmethod
    def signature(cls) -> str:
        """给 LLM 的一行函数签名。

        形如 ``push_cube(color: red|green|blue, x: number, y: number): 描述``。
        """
        schema = cls.params_model.model_json_schema()
        props: dict[str, Any] = schema.get("properties", {})
        required = set(schema.get("required", []))
        parts = []
        for field_name, spec in props.items():
            type_name = spec.get("type", "any")
            if "enum" in spec:
                type_name = "|".join(str(v) for v in spec["enum"])
            mark = "" if field_name in required else "?"
            parts.append(f"{field_name}{mark}: {type_name}")
        return f"{cls.name}({', '.join(parts)}): {cls.description}"

    @classmethod
    def tool_schema(cls) -> dict[str, Any]:
        """导出 OpenAI 兼容的 function 定义，供 Function Calling 使用。

        与 :meth:`signature` 的区别：签名是给人（和纯文本 prompt）看的一行摘要，
        这里是给模型看的**完整 JSON Schema**——包含类型、枚举、必填项与字段说明。
        模型据此填参数，``extra="forbid"`` 又让多填的参数被拒，两头都收紧。

        ``$defs`` / ``$ref`` 必须展开：部分 OpenAI 兼容网关对嵌套引用支持不一致，
        会直接忽略带 ``$ref`` 的参数，模型于是「看不到」例如 ``patrol.waypoints``
        内部结构，只能瞎猜。展开成内联是最保险的做法。
        """
        raw = cls.params_model.model_json_schema()
        parameters = _inline_refs(raw, raw.get("$defs", {}))
        parameters.pop("$defs", None)
        parameters.pop("title", None)
        parameters.setdefault("type", "object")
        return {
            "type": "function",
            "function": {
                "name": cls.name,
                "description": cls.description,
                "parameters": parameters,
            },
        }


def _compact_errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
    )


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """把 pydantic 生成的 ``$ref`` 递归展开成内联结构，并清掉纯展示用的 ``title``。

    只处理本模型自己产出的 ``#/$defs/X`` 形式；遇到指不到目标的引用原样保留，
    宁可让网关报错，也不静默丢掉字段——丢掉字段的后果是模型填错参数。
    """
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        target = defs.get(ref[len("#/$defs/") :])
        if isinstance(target, dict):
            merged = {k: v for k, v in node.items() if k != "$ref"}
            return _inline_refs({**target, **merged}, defs)

    return {
        key: _inline_refs(value, defs)
        for key, value in node.items()
        if key != "title"  # pydantic 附带的类名/字段名，对模型是噪音
    }


class SkillRegistry:
    """技能容器。线程安全：注册发生在装配阶段，执行阶段只读。"""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    # ------------------------------------------------------------------ 注册

    def register(self, skill: Skill, *, override: bool = False) -> Skill:
        if not isinstance(skill, Skill):
            raise TypeError(f"{skill!r} 不是 Skill 实例")
        if not skill.name:
            raise ValueError(f"{type(skill).__name__} 缺少类属性 name")
        if skill.name in self._skills and not override:
            raise ValueError(f"技能名重复: {skill.name}（如需覆盖请显式 override=True）")
        self._skills[skill.name] = skill
        return skill

    def register_all(self, skills: list[Skill], *, override: bool = False) -> None:
        for skill in skills:
            self.register(skill, override=override)

    def load_plugins(self, group: str = "robot_agent.skills") -> int:
        """从 entry points 加载第三方技能（插件化扩展点）。返回加载数量。"""
        loaded = 0
        for ep in entry_points(group=group):
            try:
                factory = ep.load()
                produced = factory() if callable(factory) else factory
                items = produced if isinstance(produced, (list, tuple)) else [produced]
                for item in items:
                    if isinstance(item, Skill):
                        self.register(item, override=True)
                        loaded += 1
            except Exception as exc:
                logger.warning("skill_plugin_failed", entry_point=ep.name, error=str(exc))
        return loaded

    # ------------------------------------------------------------------ 查询

    def get(self, name: str) -> Skill:
        try:
            return self._skills[name]
        except KeyError:
            raise SkillNotFoundError(
                f"未注册的技能: {name}", details={"available": sorted(self._skills)}
            ) from None

    def has(self, name: str) -> bool:
        return name in self._skills

    def names(self) -> list[str]:
        return sorted(self._skills)

    def all(self) -> list[Skill]:
        return [self._skills[n] for n in self.names()]

    def filter(self, allowed: list[str]) -> SkillRegistry:
        """按白名单派生一个只含允许技能的新注册表（用于受限场景）。"""
        if not allowed:
            return self
        subset = SkillRegistry()
        for name in allowed:
            if name in self._skills:
                subset._skills[name] = self._skills[name]
            else:
                logger.warning("allowlist_unknown_skill", skill=name)
        return subset

    def prompt_description(self) -> str:
        """给 LLM 规划器看的技能清单（纯文本模式用）。"""
        return "\n".join(skill.signature() for skill in self.all())

    def tool_schemas(self) -> list[dict[str, Any]]:
        """Function Calling 的 ``tools`` 数组。顺序按技能名稳定排序。

        顺序稳定不是洁癖：同一份输入每次请求的 tools 数组一致，上游的
        prompt 缓存（DeepSeek / Qwen 都有）才能命中，token 成本与时延都会降。
        """
        return [skill.tool_schema() for skill in self.all()]

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._skills
