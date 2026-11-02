"""一键启动：Gazebo 空世界 + TurtleBot3 + 三个彩色方块。

用法：
  export TURTLEBOT3_MODEL=waffle
  ros2 launch gazebo_langgraph_demo demo.launch.py
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node

TURTLEBOT_MODEL = os.environ.get("TURTLEBOT3_MODEL", "waffle")


def generate_launch_description() -> LaunchDescription:
    tb3_share = get_package_share_directory("turtlebot3_gazebo")
    empty_world = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(tb3_share, "launch", "empty_world.launch.py")
        )
    )
    spawn_cubes = Node(
        package="gazebo_langgraph_demo",
        executable="spawn_cubes",
        output="screen",
    )
    return LaunchDescription([empty_world, spawn_cubes])
