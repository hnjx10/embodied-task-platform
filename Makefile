# robot-agent 开发命令。Windows 下建议在 Git Bash 或 WSL 中运行。
.PHONY: help install test test-cov lint fmt type check run doctor audit-verify ros-build clean docker docker-run

PY ?= python3
INSTRUCTION ?= 把红色方块推到左边

help:
	@echo "install     安装开发依赖"
	@echo "test        跑测试"
	@echo "check       lint + type + test（CI 同款）"
	@echo "run         执行一条指令（make run INSTRUCTION='...'）"
	@echo "doctor      环境自检"
	@echo "ros-build   编译 ros2_ws 仿真辅助包（需 ROS2 环境）"

install:
	$(PY) -m pip install -e ".[dev,api]"

test:
	$(PY) -m pytest

test-cov:
	$(PY) -m pytest --cov=robot_agent --cov-report=term-missing --cov-report=html

lint:
	$(PY) -m ruff check src tests

fmt:
	$(PY) -m ruff format src tests

type:
	$(PY) -m mypy src

check: lint type test

run:
	robot-agent run "$(INSTRUCTION)"

doctor:
	robot-agent doctor

audit-verify:
	robot-agent audit verify

# 仅 Ubuntu + ROS2 环境下可用：编译世界启动与方块生成包
ros-build:
	cd ros2_ws && colcon build --symlink-install

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage build dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

docker:
	docker build -t robot-agent:local .

docker-run:
	docker run --rm -p 8080:8080 -e ROBOT_AGENT_EXECUTOR__BACKEND=mock robot-agent:local
