"""任务规划：LLM 结构化输出为主，规则解析兜底（无 API Key 演示不翻车）。"""
import json
import os
import re
from typing import Any, Dict, List

SYSTEM_PROMPT = """你是机器人任务规划器。把用户的中文指令拆解为技能调用序列，只输出 JSON：
{"steps": [{"skill": "<技能名>", "args": {<参数>}}, ...]}
可用技能：
{skills}
场地坐标范围 x,y ∈ [-1.5, 1.5]。左边=(-1.2, 0)，右边=(1.2, 0)，中间=(0, 0)。
规则：不要编造不存在的技能；推箱前如需对准可加 goto_point；只输出 JSON，不要多余文字。"""

# 左/右/中 → 坐标映射（规则兜底用）
_PRESET = {"左边": (-1.2, 0.0), "右边": (1.2, 0.0), "中间": (0.0, 0.0),
           "左": (-1.2, 0.0), "右": (1.2, 0.0), "中间位置": (0.0, 0.0)}
_COLORS = "红|绿|蓝"


def _coords_from_text(text: str):
    text = text.strip()
    if text in _PRESET:
        return _PRESET[text]
    m = re.match(r"[\(（]?\s*(-?[\d.]+)\s*[,，]\s*(-?[\d.]+)\s*[\)）]?", text)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def rule_plan(instruction: str) -> List[Dict[str, Any]]:
    """无 LLM 时的确定性解析，覆盖演示指令。"""
    steps: List[Dict[str, Any]] = []
    push_re = rf"把({_COLORS})色?方块?(?:推|搬|移)(?:到|至|向)?(.+)"
    m = re.search(push_re, instruction)
    if m:
        color = {"红": "red", "绿": "green", "蓝": "blue"}[m.group(1)]
        target = m.group(2).rstrip("。 .")
        coords = _coords_from_text(target)
        if coords:
            steps.append({"skill": "push_cube", "args": {"color": color, "x": coords[0], "y": coords[1]}})
            return steps
    goto_re = r"(?:开到|走到|移动到|导航到)(.+)"
    m = re.search(goto_re, instruction)
    if m:
        coords = _coords_from_text(m.group(1))
        if coords:
            speed = 0.5 if ("全速" in instruction or "最快" in instruction) else 0.15
            steps.append({"skill": "goto_point", "args": {"x": coords[0], "y": coords[1], "speed": speed}})
            return steps
    raise ValueError(f"规则解析失败，无法规划该指令: {instruction}")


def llm_plan(instruction: str, skills_prompt: str) -> List[Dict[str, Any]]:
    """调用 OpenAI 兼容接口做规划；未配置或解析失败时抛异常交由上层兜底。"""
    from openai import OpenAI

    client = OpenAI()  # 读 OPENAI_API_KEY / OPENAI_BASE_URL 环境变量
    model = os.environ.get("OPENAI_MODEL", "qwen-plus")
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT.format(skills=skills_prompt)},
            {"role": "user", "content": instruction},
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("LLM 返回中没有 steps")
    for s in steps:  # 最小结构校验
        if "skill" not in s or "args" not in s:
            raise ValueError(f"LLM 步骤结构非法: {s}")
    return steps


def plan(instruction: str, skills_prompt: str) -> List[Dict[str, Any]]:
    """规划入口：先 LLM，失败落规则。返回 [{skill, args}]。"""
    if os.environ.get("OPENAI_API_KEY"):
        try:
            return llm_plan(instruction, skills_prompt)
        except Exception as e:  # noqa: BLE001 —— 演示项目：LLM 失败必须兜底
            print(f"[planner] LLM 规划失败({e})，使用规则解析兜底")
    return rule_plan(instruction)
