from glob import glob

from setuptools import setup

package_name = "robot_agent_ros2"

setup(
    name=package_name,
    version="1.3.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        ("share/" + package_name + "/launch", glob("launch/*.launch.py")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Jiang Hua",
    maintainer_email="16681611657@163.com",
    description="Gazebo 场景启动与方块生成",
    license="MIT",
    entry_points={
        "console_scripts": [
            "spawn_cubes = robot_agent_ros2.spawn_cubes:main",
        ],
    },
)
