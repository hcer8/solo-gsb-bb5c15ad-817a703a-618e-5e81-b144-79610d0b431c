"""POST /batches 幂等提交（Idempotency-Key）端到端测试。"""

from __future__ import annotations

import threading


def _m(mid, irt=None, refs=None, sent="2026-09-18T10:00:00+00:00", subject="hello"):
    return {
        "message_id": mid,
        "in_reply_to": irt,
        "references": refs or [],
        "sent_at": sent,
        "subject": subject,
    }


def _batch(*mids):
    return {"messages": [
        _m(mid, sent=f"2026-09-18T{9 + i:02d}:00:00+00:00")
        for i, mid in enumerate(mids)
    ]}


# ---------------------------------------------------------------------------
# 未携带键：行为完全不变
# ---------------------------------------------------------------------------


def test_request_without_idempotency_key_keeps_existing_behavior(client):
    payload = _batch("A", "B")
    r1 = client.post("/batches", json=payload)
    r2 = client.post("/batches", json=payload)

    assert r1.status_code == 201
    assert r2.status_code == 201
    # 不带键：相同请求体各归档一次，两个独立批次
    assert r1.json()["batch_id"] != r2.json()["batch_id"]
    assert "idempotent-replay" not in r1.headers
    assert "idempotent-replay" not in r2.headers
    assert client.get("/batches").json()["total"] == 2
    # 第二次提交把 A/B 识别为历史归档重复
    assert r2.json()["totals"]["archived_duplicates"] == 2


def test_blank_idempotency_key_is_rejected_as_malformed(client):
    # 头存在但仅含空白 -> 格式不合法（422）；只有“完全不带头”才走旧行为
    r = client.post(
        "/batches", json=_batch("A"), headers={"Idempotency-Key": "   "}
    )
    assert r.status_code == 422
    assert client.get("/batches").json()["total"] == 0


# ---------------------------------------------------------------------------
# 同键同体：重放首次响应，不重复归档
# ---------------------------------------------------------------------------


def test_same_key_and_same_body_replays_first_response_without_rearchiving(client):
    payload = _batch("A", "B")
    r1 = client.post(
        "/batches", json=payload, headers={"Idempotency-Key": "key-001"}
    )
    assert r1.status_code == 201
    assert "idempotent-replay" not in r1.headers

    r2 = client.post(
        "/batches", json=payload, headers={"Idempotency-Key": "key-001"}
    )
    assert r2.status_code == 201
    assert r2.headers["idempotent-replay"] == "true"
    # 原样重放首次 BatchResult（含 batch_id / submitted_at 快照）
    assert r2.json() == r1.json()

    # 只有一个批次；GET 快照与首次响应一致
    assert client.get("/batches").json()["total"] == 1
    assert client.get(f"/batches/{r1.json()['batch_id']}").json() == r1.json()
    # 邮件只归档一次：重放不产生历史归档重复诊断
    assert r2.json()["totals"]["archived_duplicates"] == 0


def test_replay_returns_same_response_even_after_other_batches(client):
    r1 = client.post(
        "/batches", json=_batch("A"), headers={"Idempotency-Key": "k-A"}
    )
    # 键不同、内容也不同的独立提交照常归档
    client.post("/batches", json=_batch("Z"), headers={"Idempotency-Key": "k-Z"})
    client.post("/batches", json=_batch("Q"))

    r2 = client.post(
        "/batches", json=_batch("A"), headers={"Idempotency-Key": "k-A"}
    )
    assert r2.status_code == 201
    assert r2.headers["idempotent-replay"] == "true"
    assert r2.json() == r1.json()
    assert client.get("/batches").json()["total"] == 3


def test_different_keys_archive_independently(client):
    payload = _batch("A")
    r1 = client.post(
        "/batches", json=payload, headers={"Idempotency-Key": "k-1"}
    )
    r2 = client.post(
        "/batches", json=payload, headers={"Idempotency-Key": "k-2"}
    )
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["batch_id"] != r2.json()["batch_id"]
    assert client.get("/batches").json()["total"] == 2
    # 第二次为正常新提交：A 已归档，计入历史重复
    assert r2.json()["totals"]["archived_duplicates"] == 1


def test_idempotency_scope_does_not_affect_rfc5322_import(client):
    # 幂等仅作用于 POST /batches；RFC 导入路径保持原有行为
    headers = [
        "Message-ID: <imp@example.com>\r\n"
        "Date: Fri, 18 Sep 2026 10:00:00 +0000\r\n"
        "Subject: hi\r\n",
    ]
    r1 = client.post("/imports/rfc5322", json={"headers": headers})
    assert r1.status_code == 201, r1.text
    r2 = client.post("/imports/rfc5322", json={"headers": headers})
    assert r2.status_code == 201, r2.text
    assert r1.json()["batch_id"] != r2.json()["batch_id"]


# ---------------------------------------------------------------------------
# 同键不同体：409 且不写库
# ---------------------------------------------------------------------------


def test_same_key_different_body_returns_409_and_writes_nothing(client):
    r1 = client.post(
        "/batches", json=_batch("A"), headers={"Idempotency-Key": "dup-key"}
    )
    assert r1.status_code == 201

    r2 = client.post(
        "/batches", json=_batch("A", "B"), headers={"Idempotency-Key": "dup-key"}
    )
    assert r2.status_code == 409
    detail = r2.json()["detail"]
    assert detail["error"] == "idempotency_payload_mismatch"

    # 冲突请求不归档：仍只有首次的批次与邮件 A
    assert client.get("/batches").json()["total"] == 1
    assert client.get("/messages/B").status_code == 404

    # 键仍绑定首次内容：原体重放成功
    r3 = client.post(
        "/batches", json=_batch("A"), headers={"Idempotency-Key": "dup-key"}
    )
    assert r3.status_code == 201
    assert r3.json() == r1.json()


def test_body_digest_is_byte_sensitive_to_json_whitespace(client):
    # 语义相同但字节不同的 JSON（额外空白）视为不同内容 -> 409
    compact = b'{"messages":[{"message_id":"A","sent_at":"2026-09-18T09:00:00+00:00","subject":"hello"}]}'
    spaced = b'{ "messages": [ { "message_id": "A", "sent_at": "2026-09-18T09:00:00+00:00", "subject": "hello" } ] }'
    headers = {"Content-Type": "application/json", "Idempotency-Key": "byte-key"}

    r1 = client.post("/batches", content=compact, headers=headers)
    assert r1.status_code == 201
    r2 = client.post("/batches", content=spaced, headers=headers)
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "idempotency_payload_mismatch"
    # 逐字节重放成功
    r3 = client.post("/batches", content=compact, headers=headers)
    assert r3.status_code == 201
    assert r3.headers["idempotent-replay"] == "true"


# ---------------------------------------------------------------------------
# 键格式校验：422
# ---------------------------------------------------------------------------


def test_malformed_idempotency_keys_are_rejected(client):
    payload = _batch("A")
    for bad in ["bad key", "key/123", "key=1", "a@b", "key;1", "x" * 129]:
        r = client.post(
            "/batches", json=payload, headers={"Idempotency-Key": bad}
        )
        assert r.status_code == 422, bad
    # 被拒请求不写库、不占键：修正后的键可正常首次提交
    assert client.get("/batches").json()["total"] == 0
    ok = client.post(
        "/batches", json=payload, headers={"Idempotency-Key": "x" * 128}
    )
    assert ok.status_code == 201


def test_malformed_keys_with_non_ascii_bytes_rejected_at_asgi_level(client):
    # 非 ASCII / NUL 字节无法经普通 HTTP 头合法传输；直接构造 ASGI 请求
    # 验证服务端仍按 422 拒绝，不会把畸形值写入登记表。
    import asyncio
    import json

    app = client.app
    body = json.dumps(_batch("A")).encode()

    async def post_with_raw_key(raw_key: bytes) -> int:
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "path": "/batches", "raw_path": b"/batches",
            "headers": [
                (b"content-type", b"application/json"),
                (b"idempotency-key", raw_key),
            ],
            "query_string": b"",
            "client": ("testclient", 80), "server": ("testserver", 80),
            "scheme": "http",
        }
        messages = [{"type": "http.request", "body": body}]
        sent: list[dict] = []

        async def receive() -> dict:
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)

        await app(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        return start["status"]

    for bad_key in ["héllo".encode("latin-1"), b"a\x00b"]:
        status = asyncio.run(post_with_raw_key(bad_key))
        assert status == 422, bad_key
    assert client.get("/batches").json()["total"] == 0


def test_idempotency_key_is_trimmed_and_accepts_allowed_charset(client):
    payload = _batch("A")
    r1 = client.post(
        "/batches", json=payload, headers={"Idempotency-Key": "  K.1_2:3-4  "}
    )
    assert r1.status_code == 201
    # 去除首尾空白后作为登记键
    r2 = client.post(
        "/batches", json=payload, headers={"Idempotency-Key": "K.1_2:3-4"}
    )
    assert r2.status_code == 201
    assert r2.headers["idempotent-replay"] == "true"
    assert r2.json() == r1.json()


# ---------------------------------------------------------------------------
# 并发：同键同体并发请求只归档一次
# ---------------------------------------------------------------------------


def test_concurrent_requests_with_same_key_archive_exactly_once(client):
    payload = _batch("A", "B")
    results: list = []
    barrier = threading.Barrier(6)

    def worker() -> None:
        barrier.wait()
        r = client.post(
            "/batches", json=payload, headers={"Idempotency-Key": "race-key"}
        )
        results.append((r.status_code, r.json(), r.headers.get("idempotent-replay")))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 6
    assert all(status == 201 for status, _, _ in results)
    batch_ids = {body["batch_id"] for _, body, _ in results}
    assert len(batch_ids) == 1  # 只有一个批次真正归档
    # 全部响应体相同（一个首次 + 五个重放）
    first_body = results[0][1]
    assert all(body == first_body for _, body, _ in results)
    replays = sum(1 for _, _, flag in results if flag == "true")
    assert replays == 5

    assert client.get("/batches").json()["total"] == 1
    # 邮件仅归档一次，没有重复落库
    assert client.get("/messages/A").status_code == 200
    listed = client.get("/batches").json()["batches"]
    assert listed[0]["total_count"] == 2


def test_failed_first_request_does_not_retain_the_key(client):
    # 空批次在 pydantic 层即被拒（422），不进入登记；键仍可用于后续合法请求
    bad = client.post(
        "/batches", json={"messages": []}, headers={"Idempotency-Key": "retry-key"}
    )
    assert bad.status_code == 422

    ok1 = client.post(
        "/batches", json=_batch("A"), headers={"Idempotency-Key": "retry-key"}
    )
    assert ok1.status_code == 201
    ok2 = client.post(
        "/batches", json=_batch("A"), headers={"Idempotency-Key": "retry-key"}
    )
    assert ok2.status_code == 201
    assert ok2.headers["idempotent-replay"] == "true"
    assert ok2.json() == ok1.json()


def test_pending_registration_from_another_process_returns_409(client):
    # 多进程部署下，另一进程已插入 pending（本进程锁尚在处理中）：
    # 返回 409 idempotency_request_in_progress，不归档、不抢占键。
    import sqlite3
    from datetime import datetime, timezone

    from app import config

    conn = sqlite3.connect(config.settings.database_path)
    conn.execute(
        "INSERT INTO idempotency_keys (idempotency_key, request_digest, status, created_at)"
        " VALUES (?, ?, 'pending', ?)",
        ("pending-key", "x" * 64, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    r = client.post(
        "/batches", json=_batch("A"), headers={"Idempotency-Key": "pending-key"}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "idempotency_request_in_progress"
    # 没有归档任何批次；pending 行保持原样（稍后由原请求完成或回滚）
    assert client.get("/batches").json()["total"] == 0


def test_key_registration_and_archive_commit_or_roll_back_together(client, monkeypatch):
    # 归档与键登记在同一事务：归档中途异常 -> 回滚，键不残留，可立即重试。
    from app import service

    calls = {"n": 0}
    real_archive = service._archive_batch

    def flaky(conn, normalized):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated archive failure")
        return real_archive(conn, normalized)

    monkeypatch.setattr(service, "_archive_batch", flaky)

    import pytest
    with pytest.raises(RuntimeError, match="simulated archive failure"):
        client.post(
            "/batches", json=_batch("A"), headers={"Idempotency-Key": "atomic-key"}
        )
    assert client.get("/batches").json()["total"] == 0

    # 同键立刻重试（不再注入故障）：首次登记已随事务回滚，请求成功
    monkeypatch.undo()
    r = client.post(
        "/batches", json=_batch("A"), headers={"Idempotency-Key": "atomic-key"}
    )
    assert r.status_code == 201
    assert client.get("/batches").json()["total"] == 1
