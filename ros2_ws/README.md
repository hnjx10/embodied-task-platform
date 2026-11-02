# ROS2 侧场景包

只负责**起场景**（Gazebo 世界 + 机器人 + 方块），不参与任务编排。
编排在 Python 包 `robot_agent` 里，作为独立进程通过 ROS2 话题控制机器人——
两边解耦，所以换执行器（mock / 真机 / Isaac Sim）不影响编排层。

## 构建

```bash
# 需要 ROS2 Humble + turtlebot3_gazebo
source /opt/ros/humble/setup.bash
export TURTLEBOT3_MODEL=waffle
export ROS_DOMAIN_ID=42

cd ros2_ws
colcon build --symlink-install
source install/setup.bash
```

## 起场景

```bash
ros2 launch robot_agent_ros2 demo.launch.py
```

## 跑任务（另一个终端）

```bash
export ROBOT_AGENT_EXECUTOR__BACKEND=ros2
export ROBOT_AGENT_EXECUTOR__ROS_DOMAIN_ID=42
robot-agent run "把红色方块推到左边"
```

## 与 Python 配置的一致性

方块坐标与尺寸在 `robot_agent_ros2/spawn_cubes.py` 的 `CUBES` / `CUBE_SIZE` 里，
必须与 `config/default.yaml` 的 `arena.cube_layout` / `arena.cube_size` 一致，
否则 `get_pose` 读到的位姿与规划假设不符，核验会失败。
