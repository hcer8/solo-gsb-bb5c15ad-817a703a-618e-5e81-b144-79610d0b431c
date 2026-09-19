"""POST /batches 幂等提交：键格式校验与请求体摘要。

客户端可在 ``POST /batches`` 上携带 ``Idempotency-Key`` 请求头：

- 键与请求体摘要（原始 body 字节的 SHA-256）在首次请求的归档事务内
  **原子登记**，因此并发同键请求只有一个会实际归档；
- 已完成的登记：摘要相同 -> 原样重放首次 BatchResult，不重复归档；
  摘要不同 -> 409（``idempotency_payload_mismatch``）；
- 处理中（pending，仅在多进程部署、进程锁无法串行化时可能观察到）->
  409（``idempotency_request_in_progress``），调用方稍后重试即可；
- 未携带键的请求完全保持既有行为。
"""

from __future__ import annotations

import hashlib
import re

# 键长度上限（去首尾空白后）。128 足以容纳 UUID/ULID/随机 token，
# 字符集限定为无空白的常见 token 字符，避免按头值截断/注入歧义。
IDEMPOTENCY_KEY_MAX_LENGTH = 128
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


class IdempotencyKeyMalformed(Exception):
    """Idempotency-Key 头格式不合法（-> 422，不写库）。"""


class IdempotencyReplay(Exception):
    """同键同体重放：携带首次 BatchResult 快照（-> 200/201，不写库）。"""

    def __init__(self, result: dict) -> None:
        self.result = result
        super().__init__("idempotent replay of a previously completed batch")


class IdempotencyPayloadMismatch(Exception):
    """同键已登记但请求体摘要不同（-> 409，不写库）。"""


class IdempotencyInProgress(Exception):
    """同键首次请求仍在处理中（-> 409，不写库）。"""


def normalize_idempotency_key(raw: str | None) -> str | None:
    """校验并归一 Idempotency-Key 头值；未携带返回 None。

    去首尾空白后必须为 1～128 个字符，且仅含字母、数字与 ``. _ : -``，
    否则抛 :class:`IdempotencyKeyMalformed`（接口层映射为 422）。
    """

    if raw is None:
        return None
    key = raw.strip()
    if not key or len(key) > IDEMPOTENCY_KEY_MAX_LENGTH:
        raise IdempotencyKeyMalformed(
            "Idempotency-Key must be 1 to %d non-whitespace characters"
            % IDEMPOTENCY_KEY_MAX_LENGTH
        )
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise IdempotencyKeyMalformed(
            "Idempotency-Key may contain only letters, digits and '._:-'"
        )
    return key


def digest_request_body(body: bytes) -> str:
    """请求体原始字节的 SHA-256 十六进制摘要。

    重放必须逐字节复现首次请求体（JSON 字段顺序、空白差异都会导致
    摘要不同而返回 409），从而把“相同内容”判定限定为明确的字节级一致。
    """

    return hashlib.sha256(body).hexdigest()
