from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pydantic import BaseModel, SecretBytes, SecretStr  # noqa: E402

from oddish.workers.harbor.redaction import (  # noqa: E402
    redact_exact_bytes,
    redact_exact_text,
    redact_exact_value,
)


def test_redact_text_replaces_longest_match_first():
    # The longer overlapping secret must be replaced before its prefix.
    replacements = {"sk-secret": "[SHORT]", "sk-secret-long": "[LONG]"}
    out = redact_exact_text("a sk-secret-long b sk-secret c", replacements)
    assert out == "a [LONG] b [SHORT] c"


def test_redact_bytes_replaces_utf8():
    assert redact_exact_bytes(b"x sk-secret y", {"sk-secret": "[REDACTED]"}) == (
        b"x [REDACTED] y"
    )


def test_redact_value_walks_json_shapes():
    # The live-tail payload case: str / dict / list / tuple only.
    payload = {"k": "sk-secret", "list": ["ok", "sk-secret"], "tuple": ("sk-secret",)}
    out = redact_exact_value(payload, {"sk-secret": "[REDACTED]"})
    assert out == {
        "k": "[REDACTED]",
        "list": ["ok", "[REDACTED]"],
        "tuple": ("[REDACTED]",),
    }


def test_redact_value_handles_pydantic_secrets():
    # The lifecycle case: SecretStr / SecretBytes / BaseModel.
    replacements = {"sk-secret": "[REDACTED]"}
    assert (
        redact_exact_value(SecretStr("sk-secret"), replacements).get_secret_value()
        == "[REDACTED]"
    )
    assert (
        redact_exact_value(SecretBytes(b"sk-secret"), replacements).get_secret_value()
        == b"[REDACTED]"
    )


def test_redact_value_redacts_model_fields():
    class _Payload(BaseModel):
        token: str
        note: str

    out = redact_exact_value(
        _Payload(token="sk-secret", note="ok"), {"sk-secret": "[REDACTED]"}
    )
    assert out.token == "[REDACTED]"
    assert out.note == "ok"


def test_redact_value_depth_guard_truncates_without_leaking():
    # A pathologically deep tree is truncated at the recursion limit rather than
    # recursing without bound; a secret buried below the limit is collapsed away
    # (still cannot leak) rather than surviving raw.
    deep: dict = {}
    current = deep
    for _ in range(40):
        nxt: dict = {}
        current["n"] = nxt
        current = nxt
    current["secret"] = "sk-secret"

    out = redact_exact_value(deep, {"sk-secret": "[REDACTED]"})
    rendered = repr(out)
    assert "[REDACTION_DEPTH_LIMIT]" in rendered
    assert "sk-secret" not in rendered
