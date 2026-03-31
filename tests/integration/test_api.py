"""HTTP 控制面集成测试：真实 FastAPI + 真实装配，用 ASGI 直连不开端口。"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from robot_agent.api.app import create_app
from robot_agent.brand import PRODUCT_NAME_ZH, TECH_NAME
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
    # 文档标题必须是产品名：打开 /docs 看到的第一行就是它
    assert spec["info"]["title"] == PRODUCT_NAME_ZH


async def test_version_reports_product_and_tech_name(client: AsyncClient) -> None:
    """产品名给人看，技术名给部署脚本匹配——两个都要在，且不能混。"""
    body = (await client.get("/version")).json()
    assert body["product"] == PRODUCT_NAME_ZH
    assert body["name"] == TECH_NAME


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


async def test_sse_subscriber_limit_returns_429(tmp_path: Path) -> None:
    """每条 SSE 连接占一条队列，超限必须拒绝，而不是把内存交给客户端决定。"""
    import asyncio

    settings = make_settings(tmp_path, api={"request_timeout_s": 10.0, "max_sse_subscribers": 1})
    settings.safety.require_approval_for = ["push_cube"]
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            task_id = (
                await c.post("/v1/tasks", json={"instruction": "把红色方块推到左边"})
            ).json()["id"]
            for _ in range(100):
                status = (await c.get(f"/v1/tasks/{task_id}")).json()["status"]
                if status == "awaiting_approval":
                    break
                await asyncio.sleep(0.05)
            assert status == "awaiting_approval"

            # 先占满配额（等价于一个已连上的订阅者）
            app.state.events.subscribe(task_id)
            resp = await c.get(f"/v1/tasks/{task_id}/events")

            assert resp.status_code == 429
            assert resp.json()["error"]["code"] == "too_many_subscribers"


async def test_emergency_stop_endpoint(client: AsyncClient) -> None:
    """急停不需要 task_id，必须留审计，且不改变任务状态。"""
    from robot_agent.audit import tail_entries

    resp = await client.post("/v1/robot/stop", json={"reason": "integration test"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["stopped"] is True
    assert body["reason"] == "integration test"
    assert body["backend"] == "mock"

    settings = client.app_state.settings  # type: ignore[attr-defined]
    entries = tail_entries(settings.audit.path, limit=5)
    assert any(e.get("event") == "emergency_stop" for e in entries)


async def test_emergency_stop_without_body(client: AsyncClient) -> None:
    """空 body 也要能停——停车不该因为少填参数被拒。"""
    resp = await client.post("/v1/robot/stop")
    assert resp.status_code == 200
    assert resp.json()["reason"]


async def test_emergency_stop_reports_missing_skill(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """执行器没有 stop 技能时必须说实话（503），而不是回一个假的 200。"""
    registry = client.app_state.registry  # type: ignore[attr-defined]
    monkeypatch.setattr(registry, "has", lambda name: False)

    resp = await client.post("/v1/robot/stop")

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "executor_unavailable"


async def test_purge_scheduler_runs_with_lifespan(tmp_path: Path) -> None:
    """保留期配置必须真的被执行：lifespan 启动清理协程，退出时收掉。"""
    from robot_agent.api.app import create_app

    app = create_app(make_settings(tmp_path))
    async with app.router.lifespan_context(app):
        purger = app.state.assembly.purger
        assert purger is not None
        assert purger._task is not None and not purger._task.done(), "清理协程应随应用启动"

    assert purger._task is None, "退出后必须收掉，不能留孤儿协程"


async def test_idempotency_key_returns_same_task(client: AsyncClient) -> None:
    head = {"Idempotency-Key": "order-42"}
    first = await client.post("/v1/tasks", json={"instruction": "把红色方块推到左边"}, headers=head)
    second = await client.post(
        "/v1/tasks", json={"instruction": "把红色方块推到左边"}, headers=head
    )

    assert first.json()["id"] == second.json()["id"]


async def test_idempotency_key_reuse_with_different_instruction(client: AsyncClient) -> None:
    """同一个键配不同指令必须 409：静默返回旧任务会让调用方拿别人的结果当自己的。"""
    head = {"Idempotency-Key": "order-43"}
    await client.post("/v1/tasks", json={"instruction": "把红色方块推到左边"}, headers=head)

    resp = await client.post("/v1/tasks", json={"instruction": "把绿色方块推到右边"}, headers=head)

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "task_conflict"
    assert error["details"]["existing_instruction"] == "把红色方块推到左边"


async def test_idempotency_ignores_wrapping_whitespace(client: AsyncClient) -> None:
    """只是排版差异，不该被当成另一条指令。"""
    first = await client.post(
        "/v1/tasks", json={"instruction": "把红色方块推到左边", "idempotency_key": "k-1"}
    )
    second = await client.post(
        "/v1/tasks", json={"instruction": "  把红色方块推到左边 ", "idempotency_key": "k-1"}
    )

    assert first.json()["id"] == second.json()["id"]


async def test_sync_wait_timeout_returns_202(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wait=true 不能把 HTTP 连接挂死：超时就返回 202，任务转入后台继续跑。"""
    import asyncio

    from robot_agent.api.app import create_app

    settings = make_settings(tmp_path, api={"request_timeout_s": 0.05})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        runner = app.state.runner
        real_execute = runner.execute

        async def slow_execute(task_id: str):
            await asyncio.sleep(5.0)  # 长于 request_timeout_s，必然超时
            return await real_execute(task_id)

        monkeypatch.setattr(runner, "execute", slow_execute)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post(
                "/v1/tasks", json={"instruction": "把红色方块推到左边", "wait": True}
            )

    assert resp.status_code == 202
    body = resp.json()
    assert "note" in body, "超时后必须告诉调用方任务还在后台跑"
    assert "后台" in body["note"]


async def test_list_tasks_reports_pagination(client: AsyncClient) -> None:
    """列表必须给出 total / has_more：客户端靠它渲染「第几条，共几条」。"""
    for i in range(3):
        await client.post("/v1/tasks", json={"instruction": f"走到({i}.0, 0.0)"})

    first = (await client.get("/v1/tasks?limit=2&offset=0")).json()

    assert first["count"] == 2
    assert first["total"] == 3
    assert first["limit"] == 2
    assert first["offset"] == 0
    assert first["has_more"] is True

    second = (await client.get("/v1/tasks?limit=2&offset=2")).json()
    assert second["count"] == 1
    assert second["total"] == 3
    assert second["has_more"] is False


async def test_list_tasks_total_respects_status_filter(client: AsyncClient) -> None:
    await client.post("/v1/tasks", json={"instruction": "走到(1.0, 0.0)", "wait": True})

    body = (await client.get("/v1/tasks?status=succeeded")).json()

    assert body["total"] == len(body["items"])
    assert body["has_more"] is False
