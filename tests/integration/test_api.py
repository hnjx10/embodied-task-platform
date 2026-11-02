"""HTTP 控制面集成测试：真实 FastAPI + 真实装配，用 ASGI 直连不开端口。"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from robot_agent.api.app import create_app
from robot_agent.config import Settings

pytestmark = pytest.mark.integration


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = {
        "env": "test",
        "log_json": True,
        "executor": {"backend": "mock", "step_timeout_s": 5.0},
        "audit": {"enabled": True, "path": tmp_path / "audit.jsonl"},
        "storage": {"url": f"sqlite:///{tmp_path / 'tasks.db'}"},
        "api": {"request_timeout_s": 10.0},
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
async def client(tmp_path: Path):
    app = create_app(make_settings(tmp_path))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            c.app_state = app.state  # type: ignore[attr-defined]
            yield c


async def test_healthz(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_readyz(client: AsyncClient) -> None:
    resp = await client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["checks"]["executor"]["ok"] is True
    assert body["checks"]["storage"]["ok"] is True


async def test_version(client: AsyncClient) -> None:
    assert (await client.get("/version")).json()["name"] == "robot-agent"


async def test_submit_sync_success(client: AsyncClient) -> None:
    resp = await client.post("/v1/tasks", json={"instruction": "把红色方块推到左边", "wait": True})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["outcomes"][0]["outcome"] == "ok"


async def test_submit_async_then_poll(client: AsyncClient) -> None:
    resp = await client.post("/v1/tasks", json={"instruction": "把红色方块推到左边"})
    assert resp.status_code == 202
    task_id = resp.json()["id"]

    for _ in range(50):
        detail = await client.get(f"/v1/tasks/{task_id}")
        if detail.json()["status"] in {"succeeded", "failed", "blocked"}:
            break
        await __import__("asyncio").sleep(0.05)
    assert detail.json()["status"] == "succeeded"


async def test_blocked_task_reported(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/tasks", json={"instruction": "把绿色方块推到 (9, 9)", "wait": True}
    )
    assert resp.json()["status"] == "blocked"


async def test_missing_task_returns_404(client: AsyncClient) -> None:
    resp = await client.get("/v1/tasks/task_missing")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "task_not_found"


async def test_empty_instruction_rejected(client: AsyncClient) -> None:
    resp = await client.post("/v1/tasks", json={"instruction": ""})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_extra_field_rejected(client: AsyncClient) -> None:
    resp = await client.post("/v1/tasks", json={"instruction": "x", "unknown": 1})
    assert resp.status_code == 422


async def test_idempotency(client: AsyncClient) -> None:
    first = await client.post(
        "/v1/tasks",
        json={"instruction": "把红色方块推到左边", "idempotency_key": "k-api-1"},
    )
    second = await client.post(
        "/v1/tasks",
        json={"instruction": "把红色方块推到左边", "idempotency_key": "k-api-1"},
    )
    assert first.json()["id"] == second.json()["id"]


async def test_idempotency_via_header(client: AsyncClient) -> None:
    headers = {"Idempotency-Key": "k-header-1"}
    payload = {"instruction": "把红色方块推到左边"}
    a = await client.post("/v1/tasks", json=payload, headers=headers)
    b = await client.post("/v1/tasks", json=payload, headers=headers)
    assert a.json()["id"] == b.json()["id"]


async def test_list_tasks(client: AsyncClient) -> None:
    await client.post("/v1/tasks", json={"instruction": "把红色方块推到左边", "wait": True})
    resp = await client.get("/v1/tasks?limit=10")
    assert resp.status_code == 200
    assert resp.json()["count"] >= 1


async def test_approval_flow(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        settings.safety.require_approval_for = ["push_cube"]
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            created = await c.post("/v1/tasks", json={"instruction": "把红色方块推到左边"})
            task_id = created.json()["id"]
            for _ in range(100):
                detail = await c.get(f"/v1/tasks/{task_id}")
                if detail.json()["status"] in {"awaiting_approval", "succeeded", "failed"}:
                    break
                await __import__("asyncio").sleep(0.05)
            assert detail.json()["status"] == "awaiting_approval"

            approved = await c.post(f"/v1/tasks/{task_id}/approve", json={"approved_by": "qa"})
            assert approved.status_code == 200
            assert approved.json()["status"] == "succeeded"

            again = await c.post(f"/v1/tasks/{task_id}/approve", json={"approved_by": "qa2"})
            assert again.status_code == 409


async def test_reject_flow(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        settings.safety.require_approval_for = ["push_cube"]
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            task_id = (
                await c.post("/v1/tasks", json={"instruction": "把红色方块推到左边"})
            ).json()["id"]
            await __import__("asyncio").sleep(0.2)
            resp = await c.post(f"/v1/tasks/{task_id}/reject", json={"reason": "现场有人"})
            assert resp.status_code == 200
            assert resp.json()["status"] == "blocked"


async def test_cancel_awaiting_task(tmp_path: Path) -> None:
    """取消的典型场景是「待审批但现场情况变了」，而不是打断正在执行的动作。"""
    settings = make_settings(tmp_path)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        settings.safety.require_approval_for = ["push_cube"]
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            task_id = (
                await c.post("/v1/tasks", json={"instruction": "把红色方块推到左边"})
            ).json()["id"]
            for _ in range(100):
                detail = await c.get(f"/v1/tasks/{task_id}")
                if detail.json()["status"] in {"awaiting_approval", "succeeded"}:
                    break
                await __import__("asyncio").sleep(0.05)
            resp = await c.post(f"/v1/tasks/{task_id}/cancel", json={"reason": "测试"})
            assert resp.status_code == 200
            assert resp.json()["status"] == "cancelled"


async def test_cannot_cancel_running_motion(client: AsyncClient) -> None:
    """执行中的任务不能被 cancel 静默打断，必须走急停——否则等于绕过安全流程。"""
    await client.post("/v1/tasks", json={"instruction": "把红色方块推到左边", "wait": True})
    resp = await client.post("/v1/tasks/task_missing/cancel", json={"reason": "x"})
    assert resp.status_code == 404


async def test_auth_required(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, api={"auth_token": "s3cret", "request_timeout_s": 10.0})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            assert (
                await c.post("/v1/tasks", json={"instruction": "把红色方块推到左边"})
            ).status_code == 401
            ok = await c.post(
                "/v1/tasks",
                json={"instruction": "把红色方块推到左边", "wait": True},
                headers={"Authorization": "Bearer s3cret"},
            )
            assert ok.status_code == 202
            bad = await c.post(
                "/v1/tasks",
                json={"instruction": "x"},
                headers={"Authorization": "Bearer wrong"},
            )
            assert bad.status_code == 401


async def test_trace_header_echoed(client: AsyncClient) -> None:
    resp = await client.get("/healthz", headers={"X-Trace-Id": "abc123"})
    assert resp.headers["X-Trace-Id"] == "abc123"


async def test_trace_header_generated(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert len(resp.headers["X-Trace-Id"]) == 16


async def test_metrics_endpoint(client: AsyncClient) -> None:
    await client.post("/v1/tasks", json={"instruction": "把红色方块推到左边", "wait": True})
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "robot_agent_tasks_total" in resp.text


async def test_openapi_schema(client: AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    assert "/v1/tasks" in spec["paths"]
    assert spec["info"]["title"] == "robot-agent"


async def test_sse_stream_closes_on_terminal(client: AsyncClient) -> None:
    task_id = (
        await client.post("/v1/tasks", json={"instruction": "把红色方块推到左边", "wait": True})
    ).json()["id"]
    async with client.stream("GET", f"/v1/tasks/{task_id}/events") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        chunk = b""
        async for part in resp.aiter_bytes():
            chunk += part
            if b"event: end" in chunk:
                break
    assert b"event: status" in chunk


async def test_oversized_body_rejected(client: AsyncClient) -> None:
    """声明就超限的请求体，应在读 body 之前被 413 拒掉。"""
    huge = "把红色方块推到左边" + "啊" * 40000  # ≈120 KB UTF-8，远超 64 KiB 上限
    resp = await client.post("/v1/tasks", json={"instruction": huge, "wait": False})
    assert resp.status_code == 413
    error = resp.json()["error"]
    assert error["code"] == "payload_too_large"
    assert error["details"]["limit_bytes"] == 64 * 1024


async def test_body_limit_configured_per_app(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, api={"request_timeout_s": 10.0, "max_request_bytes": 1024})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/v1/tasks", json={"instruction": "x" * 2000, "wait": False})
            assert resp.status_code == 413
            assert resp.json()["error"]["details"]["limit_bytes"] == 1024
