"""在 Gazebo 中生成三个彩色方块（red / green / blue），供推箱任务使用。

方块尺寸与坐标与 robot_agent 的 ArenaSettings 保持一致：
  - 边长 0.1 m，初始高度 0.05 m（半高，保证贴地）
  - red (0.8, 0.5) / green (0.9, -0.4) / blue (-0.7, 0.6)

改场地配置时**两处都要改**：config/default.yaml 的 arena.cube_layout 与这里的 CUBES。
不一致会导致 get_pose 读到的初始位姿与规划假设不同。
"""

from __future__ import annotations

import sys

try:
    import rclpy
    from gazebo_msgs.srv import SpawnEntity
except ImportError:  # pragma: no cover
    print("需要 ROS2 环境：source /opt/ros/humble/setup.bash", file=sys.stderr)
    raise SystemExit(2) from None

CUBE_SIZE = 0.1
CUBES = {
    "red_cube": (0.8, 0.5),
    "green_cube": (0.9, -0.4),
    "blue_cube": (-0.7, 0.6),
}

CUBE_SDF = """<?xml version="1.0"?>
<sdf version="1.6">
  <model name="{name}">
    <pose>{x} {y} {z} 0 0 0</pose>
    <link name="link">
      <inertial>
        <mass>0.08</mass>
        <inertia>
          <ixx>0.0001</ixx><iyy>0.0001</iyy><izz>0.0001</izz>
          <ixy>0</ixy><ixz>0</ixz><iyz>0</iyz>
        </inertia>
      </inertial>
      <collision name="collision">
        <geometry><box><size>{size} {size} {size}</size></box></geometry>
        <surface><friction><ode><mu>1.0</mu><mu2>1.0</mu2></ode></friction></surface>
      </collision>
      <visual name="visual">
        <geometry><box><size>{size} {size} {size}</size></box></geometry>
        <material>
          <ambient>{r} {g} {b} 1</ambient>
          <diffuse>{r} {g} {b} 1</diffuse>
        </material>
      </visual>
    </link>
  </model>
</sdf>
"""

COLORS = {
    "red_cube": (0.8, 0.1, 0.1),
    "green_cube": (0.1, 0.7, 0.1),
    "blue_cube": (0.1, 0.2, 0.8),
}


def main() -> int:
    rclpy.init()
    node = rclpy.create_node("spawn_cubes")
    client = node.create_client(SpawnEntity, "/spawn_entity")

    if not client.wait_for_service(timeout_sec=10.0):
        node.get_logger().error("未找到 /spawn_entity 服务，请先启动 Gazebo")
        node.destroy_node()
        rclpy.shutdown()
        return 1

    half = CUBE_SIZE / 2.0
    for name, (x, y) in CUBES.items():
        r, g, b = COLORS[name]
        request = SpawnEntity.Request()
        request.name = name
        request.xml = CUBE_SDF.format(name=name, x=x, y=y, z=half, size=CUBE_SIZE, r=r, g=g, b=b)
        request.initial_pose.position.x = x
        request.initial_pose.position.y = y
        request.initial_pose.position.z = half

        future = client.call_async(request)
        rclpy.spin_until_future_complete(node, future, timeout_sec=10.0)
        if future.result() is not None and future.result().success:
            node.get_logger().info(f"已生成 {name} @ ({x}, {y})")
        else:
            node.get_logger().error(f"生成 {name} 失败: {future.result()}")

    node.destroy_node()
    rclpy.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
