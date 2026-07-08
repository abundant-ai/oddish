from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
import pytest
import typer

from oddish.cli.logs import _TransientError, _fetch, _render_event, stream_logs


def event(seq, kind="message", **payload):
    return {"seq": seq, "kind": kind, "payload": payload}


def page(attempt, events, *, done=False, cost=0.5):
    return {
        "attempt": attempt,
        "events": events,
        "next_seq": events[-1]["seq"] if events else 0,
        "usage": {"input_tokens": 100, "output_tokens": 20, "cost_usd": cost},
        "done": done,
    }


class FakeFetch:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, attempt, after_seq):
        self.calls.append((attempt, after_seq))
        assert self.responses, "fetch called more times than scripted"
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run(responses, *, follow):
    fetch = FakeFetch(responses)
    lines: list[str] = []
    sleeps: list[float] = []
    seen = stream_logs(
        fetch, follow=follow, emit=lines.append, sleep=sleeps.append
    )
    return seen, fetch, lines, sleeps


def test_render_event_kinds_and_noise():
    assert _render_event(event(1, text="hello")) == "hello"
    assert _render_event(event(1, kind="tool_use", name="Bash", input='{"x":1}')).startswith(
        "[cyan]⚙ Bash[/cyan]"
    )
    assert _render_event(event(1, kind="tool_result", content="out")).startswith("[dim]")
    assert "[red]" in _render_event(event(1, kind="tool_result", content="boom", is_error=True))
    assert _render_event(event(1, text="")) is None
    assert _render_event(event(1, kind="tool_result", content="")) is None
    assert _render_event(event(1, kind="summary", text="done")) == "[green]done[/green]"
    assert _render_event(
        event(1, kind="summary", text="done", truncated=True)
    ).endswith(" [dim]…[/dim]")


def test_render_event_escapes_markup():
    assert _render_event(event(1, text="a [b] c")) == "a \\[b] c"


def test_single_shot_drains_pages_and_advances_cursor():
    seen, fetch, lines, sleeps = run(
        [
            page(1, [event(1), event(2), event(3)]),
            page(1, []),
        ],
        follow=False,
    )
    assert seen == 3
    assert fetch.calls == [(None, 0), (1, 3)]
    assert sleeps == []


def test_single_shot_no_events_returns_zero():
    seen, fetch, lines, sleeps = run([page(1, [])], follow=False)
    assert seen == 0
    assert fetch.calls == [(None, 0)]


def test_newer_attempt_resets_cursor_to_zero():
    seen, fetch, lines, sleeps = run(
        [
            page(1, [event(1)]),
            page(2, []),
            page(2, [], done=True),
        ],
        follow=True,
    )
    assert fetch.calls == [(None, 0), (1, 1), (2, 0)]
    assert sum("restarting transcript" in line for line in lines) == 1


def test_done_with_full_page_drains_until_empty():
    seen, fetch, lines, sleeps = run(
        [
            page(1, [event(1), event(2)], done=True),
            page(1, [], done=True),
        ],
        follow=True,
    )
    assert seen == 2
    assert fetch.calls == [(None, 0), (1, 2)]
    assert sleeps == []


def test_follow_sleeps_only_on_empty_live_page():
    seen, fetch, lines, sleeps = run(
        [
            page(1, [event(1)], done=False),
            page(1, [], done=False),
            page(1, [event(2)], done=True),
            page(1, [], done=True),
        ],
        follow=True,
    )
    assert seen == 2
    assert sleeps == [2.0]


def test_cost_line_printed_once_per_change():
    seen, fetch, lines, sleeps = run(
        [
            page(1, [event(1)], cost=0.5),
            page(1, [event(2)], cost=0.5),
            page(1, [event(3)], cost=0.9),
            page(1, []),
        ],
        follow=False,
    )
    cost_lines = [line for line in lines if "tokens" in line]
    assert len(cost_lines) == 2


def test_follow_rides_out_transient_errors():
    seen, fetch, lines, sleeps = run(
        [
            _TransientError("503"),
            httpx.ReadTimeout("boom"),
            page(1, [event(1)], done=False),
            page(1, [], done=True),
        ],
        follow=True,
    )
    assert seen == 1
    assert sleeps == [2.0, 2.0]
    assert fetch.calls == [(None, 0), (None, 0), (None, 0), (1, 1)]


def test_non_follow_transient_error_exits():
    fetch = FakeFetch([_TransientError("503")])
    with pytest.raises(typer.Exit) as exc:
        stream_logs(fetch, follow=False, emit=lambda _l: None, sleep=lambda _s: None)
    assert exc.value.exit_code == 1


def test_non_follow_httpx_error_propagates():
    fetch = FakeFetch([httpx.ConnectError("down")])
    with pytest.raises(httpx.HTTPError):
        stream_logs(fetch, follow=False, emit=lambda _l: None, sleep=lambda _s: None)


class FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        return self._json


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.params = None

    def get(self, url, params=None):
        self.params = params
        return self.response


def test_fetch_404_raises_exit():
    fetch = _fetch(FakeClient(FakeResponse(404)), "http://x", "trial-1")
    with pytest.raises(typer.Exit) as exc:
        fetch(None, 0)
    assert exc.value.exit_code == 1


def test_fetch_5xx_raises_transient():
    fetch = _fetch(FakeClient(FakeResponse(503, text="unavailable")), "http://x", "t")
    with pytest.raises(_TransientError):
        fetch(None, 0)


def test_fetch_non_retryable_status_exits():
    fetch = _fetch(FakeClient(FakeResponse(400, text="bad")), "http://x", "t")
    with pytest.raises(typer.Exit) as exc:
        fetch(None, 0)
    assert exc.value.exit_code == 1


def test_fetch_omits_attempt_when_none():
    client = FakeClient(FakeResponse(200, page(1, [])))
    fetch = _fetch(client, "http://x", "trial-1")
    fetch(None, 5)
    assert client.params == {"after_seq": 5}
    fetch(2, 7)
    assert client.params == {"after_seq": 7, "attempt": 2}
