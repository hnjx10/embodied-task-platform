#!/usr/bin/env python3
"""在 Gazebo 中生成三个彩色方块（red/green/blue），供推箱任务使用。

用法：先启动 turtlebot3_gazebo empty_world，再运行本脚本。
"""
import sys

import rclpy
from gazebo_msgs.srv import SpawnEntity

CUBE_SDF = """<?xml version="1.0"?>
<sdf version="1.6">
  <model name="{name}">
    <pose>{x} {y} 0.05 0 0 0</pose>
    <link name="link">
      <inertial>
        <mass>0.08</mass>
        <inertia><ixx>0.0001</ixx><iyy>0.0001</iyy><izz>0.0001</izz></inertia>
      </inertial>
      <collision name="collision">
        <geometry><box><size>0.08 0.08 0.08</size></box></geometry>
        <surface><friction><ode><mu>0.6</mu><mu2>0.6</mu2></ode></friction></surface>
      </collision>
      <visual name="visual">
        <geometry><box><size>0.08 0.08 0.08</size></box></geometry>
        <material><ambient>{rgba}</ambient><diffuse>{rgba}</diffuse></material>
      </visual>
    </link>
  </model>
</sdf>"""

CUBES = [
    ("red_cube", 0.8, 0.5, "1 0.1 0.1 1"),
    ("green_cube", 0.9, -0.4, "0.1 1 0.2 1"),
    ("blue_cube", -0.7, 0.6, "0.1 0.3 1 1"),
]


def main() -> int:
    rclpy.init()
    node = rclpy.create_node("spawn_cubes")
    cli = node.create_client(SpawnEntity, "/spawn_entity")
    if not cli.wait_for_service(timeout_sec=15.0):
        print("等待 /spawn_entity 服务超时，请确认 Gazebo 已启动")
        return 1
    for name, x, y, rgba in CUBES:
        req = SpawnEntity.Request()
        req.name = name
        req.xml = CUBE_SDF.format(name=name, x=x, y=y, rgba=rgba)
        future = cli.call_async(req)
        rclpy.spin_until_future_complete(node, future)
        print(f"{name}: {future.result().status_message}")
    node.destroy_node()
    rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
