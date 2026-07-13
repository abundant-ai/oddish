from __future__ import annotations

import json

import modal_app


def test_meta_super_nova_default_concurrency_is_80() -> None:
    overrides = json.loads(modal_app.MODEL_CONCURRENCY_OVERRIDES)

    assert overrides["meta/redacted_model"] == 80
