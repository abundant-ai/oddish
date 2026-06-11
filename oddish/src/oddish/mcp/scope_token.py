"""Signed, expiring URL tokens that scope the S3 MCP server to one experiment.

Harbor does not forward env to HTTP MCP servers, so the experiment scope must
ride in the URL. We sign ``{experiment_id, exp}`` with HMAC-SHA256 so a probe
agent cannot edit the URL to read another experiment's data.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_experiment_token(
    experiment_id: str, *, key: str, ttl_seconds: int = 86_400, now: int
) -> str:
    payload = {"e": experiment_id, "x": now + ttl_seconds}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(key.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def verify_experiment_token(token: str, *, key: str, now: int) -> str:
    try:
        body, sig = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("malformed token") from exc
    expected = hmac.new(key.encode(), body.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64d(sig), expected):
        raise ValueError("bad signature")
    payload = json.loads(_b64d(body))
    if now >= int(payload["x"]):
        raise ValueError("token expired")
    return str(payload["e"])
