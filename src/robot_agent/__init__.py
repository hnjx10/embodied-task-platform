"""robot-agent：面向 ROS2 / Gazebo 机器人的 LLM 任务编排服务。

分层结构::

    cli / api          ← 交付层（命令行人机接口、HTTP 控制面）
    orchestration      ← 编排层（LangGraph 状态机、任务运行器）
    planner / safety   ← 领域层（自然语言→技能序列、策略化安全闸门）
    skills / storage   ← 能力层（ROS2 与 mock 执行器、任务持久化）
    config / logging / metrics / audit / errors ← 基础设施
"""

from robot_agent.version import __version__

__all__ = ["__version__"]
