# syntax=docker/dockerfile:1
# ---------- 构建阶段 ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
# 先只拷贝依赖描述，让依赖层可缓存
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --prefix=/install ".[api]"

# ---------- 运行阶段 ----------
FROM python:3.12-slim AS runtime

# 非 root 运行：容器逃逸的影响面从「宿主」降到「一个普通用户」
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ROBOT_AGENT_CONFIG=/app/config/default.yaml \
    ROBOT_AGENT_STORAGE__URL=sqlite:////var/lib/robot-agent/tasks.db \
    ROBOT_AGENT_AUDIT__PATH=/var/lib/robot-agent/audit/audit.jsonl \
    ROBOT_AGENT_API__HOST=0.0.0.0 \
    ROBOT_AGENT_LOG_JSON=true

COPY --from=builder /install /usr/local
WORKDIR /app
COPY --chown=app:app config ./config

# 数据目录归 app 所有，便于挂载卷后仍可写
RUN mkdir -p /var/lib/robot-agent/audit && chown -R app:app /var/lib/robot-agent

USER app
EXPOSE 8080

# 用 readiness 而不是 liveness 做容器健康检查：存储没准备好时不应接收流量
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=4).status==200 else 1)"

ENTRYPOINT ["robot-agent"]
CMD ["serve"]
