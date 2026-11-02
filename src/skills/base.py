"""Skill 基类与注册表：统一 skill 名 + 参数 schema，新增技能零改动主流程。"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Type


@dataclass
class SkillResult:
    ok: bool
    detail: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


class Skill:
    """所有机器人能力的抽象基类。ROS / mock 执行器各自实现同名技能。"""

    name: str = ""
    description: str = ""
    # {"参数名": "类型说明"}，同时作为 LLM 规划的函数描述与运行时校验依据
    params_schema: Dict[str, str] = {}

    def validate_params(self, args: Dict[str, Any]) -> None:
        missing = [k for k in self.params_schema if k not in args]
        if missing:
            raise ValueError(f"skill '{self.name}' 缺少参数: {missing}")

    def execute(self, **kwargs) -> SkillResult:
        raise NotImplementedError


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill:
        if name not in self._skills:
            raise KeyError(f"未注册的技能: {name}，可用: {list(self._skills)}")
        return self._skills[name]

    def names(self) -> List[str]:
        return list(self._skills)

    def prompt_description(self) -> str:
        """给 LLM 规划器看的技能清单（类 Function Calling 描述）。"""
        lines = []
        for s in self._skills.values():
            params = ", ".join(f"{k}: {v}" for k, v in s.params_schema.items())
            lines.append(f"- {s.name}({params}): {s.description}")
        return "\n".join(lines)
