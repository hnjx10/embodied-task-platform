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

    # gazebo 的 empty_world 只发 odom → base_footprint（diff_drive 插件给的），
    # 没有 map 这一层。补一个 map → odom 的静态单位变换，目的有两个：
    # 1) tf 树从 map 到 base_link 连续，RViz 与 nav 工具链不会因为「找不到 map」
    #    直接报错；2) 规划侧用的是 odom 相对坐标，map 只是显示与对齐用的参考系，
    #    这里没有 SLAM，所以只能是单位阵——写死比留空好，留空是「不知道自己不知道」。
    # 真机上接 AMCL/SLAM 时，这个节点要换成定位系统发布的 map → odom 动态变换。
    tf_map_odom = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="tf_map_to_odom",
        arguments=["--frame-id", "map", "--child-frame-id", "odom"],
        output="screen",
    )

    return LaunchDescription([world, TimerAction(period=5.0, actions=[spawn]), tf_map_odom])
