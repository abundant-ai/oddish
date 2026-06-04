"""Tests for _upsert_comment_with_retry.

PR-comment writes are fired from per-trial/analysis/verdict hooks and were
best-effort: a single transient failure silently dropped the update and the
comment could freeze behind the real state. The retry helper makes each write
(including the terminal verdict refresh) durable against transient failures.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

notifier = importlib.import_module("oddish.integrations.github.notifier")


class _Client:
    """Fake GitHub client whose upsert returns a scripted sequence."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    async def upsert_oddish_comment(self, *, owner, repo, pr_number, body):
        self.calls += 1
        r = self._results[min(self.calls - 1, len(self._results) - 1)]
        if isinstance(r, Exception):
            raise r
        return r


def _run(client, **kw):
    slept = []

    async def fake_sleep(d):
        slept.append(d)

    result = asyncio.run(
        notifier._upsert_comment_with_retry(
            client, owner="o", repo="r", pr_number=1, body="b",
            base_delay=1.0, sleep=fake_sleep, **kw,
        )
    )
    return result, slept


def test_succeeds_first_try_no_sleep():
    c = _Client([{"id": 1}])
    result, slept = _run(c)
    assert result == {"id": 1}
    assert c.calls == 1
    assert slept == []


def test_retries_falsy_then_succeeds():
    # client swallows HTTP errors -> returns None; retry must treat that as retryable
    c = _Client([None, None, {"id": 9}])
    result, slept = _run(c)
    assert result == {"id": 9}
    assert c.calls == 3
    assert slept == [1.0, 2.0]  # exponential backoff between the 3 attempts


def test_retries_exception_then_succeeds():
    c = _Client([RuntimeError("boom"), {"id": 7}])
    result, slept = _run(c)
    assert result == {"id": 7}
    assert c.calls == 2
    assert slept == [1.0]


def test_gives_up_after_attempts():
    c = _Client([None])  # always None (e.g. persistent 404)
    result, slept = _run(c, attempts=4)
    assert result is None
    assert c.calls == 4
    assert slept == [1.0, 2.0, 4.0]  # slept between attempts, not after the last
