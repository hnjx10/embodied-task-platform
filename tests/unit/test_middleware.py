"""纯 ASGI 中间件测试。

请求体上限不能只依赖 ``Content-Length``：chunked 传输没有这个头，头也可以撒谎。
这里直接以 ASGI 最小调用方式驱动中间件，验证「边读边计数」这条兜底路径——
走 HTTP 客户端是测不到的，httpx 总会老实填上正确的长度。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from robot_agent.api.app import BodyLimitMiddleware

Scope = dict[str, Any]


async def _echo_app(scope: Scope, receive: Any, send: Any) -> None:
    """模拟一个「读完 body 再响应」的应用。"""
    while True:
        message = await receive()
        if message["type"] != "http.request" or not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _scope(headers: list[tuple[bytes, bytes]] | None = None) -> Scope:
    return {"type": "http", "method": "POST", "path": "/v1/tasks", "headers": headers or []}


async def _drive(
    middleware: BodyLimitMiddleware, chunks: list[bytes], headers: list[tuple[bytes, bytes]] | None
) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    index = {"i": 0}

    async def receive() -> dict[str, Any]:
        i = index["i"]
        index["i"] = i + 1
        body = chunks[i] if i < len(chunks) else b""
        return {"type": "http.request", "body": body, "more_body": i < len(chunks) - 1}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await middleware(_scope(headers), receive, send)
    return sent


async def test_lying_content_length_is_caught_while_reading() -> None:
    """声明 10 字节、实际喂 2 KB：必须靠读取计数发现并 413。"""
    middleware = BodyLimitMiddleware(_echo_app, max_bytes=1024)
    sent = await _drive(
        middleware,
        [b"x" * 1024, b"x" * 1024],
        headers=[(b"content-length", b"10")],
    )

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413
    payload = json.loads(sent[1]["body"])
    assert payload["error"]["code"] == "payload_too_large"
    assert payload["error"]["details"]["limit_bytes"] == 1024
    assert payload["error"]["details"]["received_bytes"] > 1024


async def test_declared_length_over_limit_rejected_before_reading() -> None:
    middleware = BodyLimitMiddleware(_echo_app, max_bytes=1024)
    sent = await _drive(
        middleware,
        [b"x"],
        headers=[(b"content-length", b"999999")],
    )

    assert sent[0]["status"] == 413
    payload = json.loads(sent[1]["body"])
    # 没读过 body，所以 received_bytes 是声明值
    assert payload["error"]["details"]["received_bytes"] == 999999


async def test_small_body_passes_through() -> None:
    middleware = BodyLimitMiddleware(_echo_app, max_bytes=1024)
    sent = await _drive(middleware, [b"hello"], headers=[(b"content-length", b"5")])

    assert sent[0]["status"] == 200
    assert sent[1]["body"] == b"ok"


async def test_non_http_scope_untouched() -> None:
    """lifespan / websocket 不该被这层碰。"""
    seen: list[str] = []

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    middleware = BodyLimitMiddleware(app, max_bytes=1)
    await middleware({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]


@pytest.mark.parametrize("raw", [b"abc", b"-5", b""])
async def test_bogus_content_length_falls_back_to_counting(raw: bytes) -> None:
    """Content-Length 不是合法数字时不能崩，退回读取计数。"""
    middleware = BodyLimitMiddleware(_echo_app, max_bytes=8)
    sent = await _drive(middleware, [b"x" * 32], headers=[(b"content-length", raw)])

    assert sent[0]["status"] == 413
