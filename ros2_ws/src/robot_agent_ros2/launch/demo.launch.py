"""一键起场景：Gazebo 空世界 + TurtleBot3 + 三个彩色方块。

    export TURTLEBOT3_MODEL=waffle
    export ROS_DOMAIN_ID=42
    colcon build --symlink-install && source install/setup.bash
    ros2 launch robot_agent_ros2 demo.launch.py

注意：方块生成节点带 5s 延迟，等 Gazebo 服务就绪后再调用 /spawn_entity，
否则首次启动会出现 "service not available" 竞态。
"""

from __future__ import annotations

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription, TimerAction
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    turtlebot_model = os.environ.get("TURTLEBOT3_MODEL", "waffle")
    os.environ["TURTLEBOT3_MODEL"] = turtlebot_model

    tb3_share = get_package_share_directory("turtlebot3_gazebo")
    world = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(tb3_share, "launch", "empty_world.launch.py")),
        launch_arguments={"use_sim_time": "true"}.items(),
    )

    spawn = Node(
        package="robot_agent_ros2",
        executable="spawn_cubes",
        name="spawn_cubes",
        output="screen",
    )

    return LaunchDescription([world, TimerAction(period=5.0, actions=[spawn])])
