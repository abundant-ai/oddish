#!/usr/bin/env python3
"""oddish-query — read-only CLI for the cc_chat global scope.

Runs inside the chat sandbox. Reads ODDISH_API_BASE_URL / ODDISH_API_KEY from
env and calls the oddish backend read API, projecting + budgeting output so the
agent stays shallow by default. stdlib only — no third-party deps.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 16000
LOG_HEAD = 4000
LOG_TAIL = 4000


def _print(line: str) -> None:  # seam for tests
    print(line)


def _die(msg: str, status: int) -> None:
    _print(json.dumps({"error": msg, "status": status}))
    sys.exit(1)


def _get(path: str, params: dict | None = None):
    base = os.environ.get("ODDISH_API_BASE_URL", "").rstrip("/")
    key = os.environ.get("ODDISH_API_KEY", "")
    url = f"{base}{path}"
    if params:
        # Only None / empty-string are excluded; 0 and other falsy values pass through.
        q = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")}
        )
        if q:
            url = f"{url}?{q}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _die("session credential expired" if e.code == 401 else str(e.reason), e.code)
    except Exception as e:
        _die(str(e), 0)


def _emit_rows(rows: list[dict]) -> bool:
    """Emit projected rows; return True if output was truncated for budget."""
    total, shown = 0, 0
    for row in rows:
        line = json.dumps(row, separators=(",", ":"))
        if total + len(line) > MAX_BYTES:
            _print(json.dumps({"_truncated": True, "_shown": shown}))
            return True
        _print(line)
        total += len(line)
        shown += 1
    return False


def _card(item: dict) -> dict:
    rt = item.get("reward_total") or 0
    rs = item.get("reward_success") or 0
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "tags": [t.get("name") for t in (item.get("user_tags") or [])],
        "total_trials": item.get("total_trials"),
        "pass_rate": round(rs / rt, 3) if rt else None,
        "last_run_at": item.get("last_run_at"),
    }


def _cmd_search(a) -> None:
    data = _get("/tasks/browse", {
        "query": a.q,
        "tags": a.tags_all,
        "tags_any": a.tags_any,
        "tags_none": a.tags_none,
        "limit": a.limit,
        "offset": a.offset,
    })
    truncated = _emit_rows([_card(i) for i in (data.get("items") or [])])
    if not truncated and data.get("has_more"):
        _print(json.dumps({"_has_more": True}, separators=(",", ":")))


def _cmd_get(a) -> None:
    _print(json.dumps(_get(f"/tasks/{a.id}/detail"), separators=(",", ":"))[:MAX_BYTES])


def _cmd_trials(a) -> None:
    data = _get(f"/tasks/{a.id}/trials")
    rows = data if isinstance(data, list) else (data.get("trials") or data.get("items") or [])
    _emit_rows([
        {
            "trial_id": t.get("id") or t.get("trial_id"),
            "status": t.get("status"),
            "reward": t.get("reward"),
        }
        for t in rows
    ])


def _cmd_logs(a) -> None:
    path = f"/trials/{a.trial_id}/" + ("trajectory" if a.trajectory else "logs")
    data = _get(path)
    text = data if isinstance(data, str) else json.dumps(data)
    if len(text) > LOG_HEAD + LOG_TAIL:
        text = text[:LOG_HEAD] + "\n…[truncated]…\n" + text[-LOG_TAIL:]
    _print(text)


def _cmd_experiment_trials(a) -> None:
    data = _get(f"/experiments/{a.exp_id}/trials")
    rows = data if isinstance(data, list) else (data.get("items") or [])
    _emit_rows([
        {
            "trial_id": t.get("trial_id"),
            "task": t.get("task_name"),
            "status": t.get("status"),
            "reward": t.get("reward"),
            "probe": t.get("is_probe"),
            "has_trajectory": t.get("has_trajectory"),
        }
        for t in rows
    ])


def _cmd_result(a) -> None:
    _print(json.dumps(_get(f"/trials/{a.trial_id}/result"), separators=(",", ":"))[:MAX_BYTES])


def _cmd_files(a) -> None:
    data = _get(f"/trials/{a.trial_id}/files", {
        "prefix": a.prefix,
        "recursive": "true" if a.recursive else None,
    })
    _print(json.dumps(data, separators=(",", ":"))[:MAX_BYTES])


def _cmd_file(a) -> None:
    data = _get(f"/trials/{a.trial_id}/files/{a.path}")
    text = data if isinstance(data, str) else json.dumps(data)
    if len(text) > LOG_HEAD + LOG_TAIL:
        text = text[:LOG_HEAD] + "\n…[truncated]…\n" + text[-LOG_TAIL:]
    _print(text)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="oddish-query")
    sub = p.add_subparsers(dest="group", required=True)

    tasks = sub.add_parser("tasks").add_subparsers(dest="cmd", required=True)
    s = tasks.add_parser("search")
    s.add_argument("--q", default=None)
    s.add_argument("--tags-all", dest="tags_all", default=None)
    s.add_argument("--tags-any", dest="tags_any", default=None)
    s.add_argument("--tags-none", dest="tags_none", default=None)
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--offset", type=int, default=0)
    s.set_defaults(func=_cmd_search)
    g = tasks.add_parser("get")
    g.add_argument("id")
    g.set_defaults(func=_cmd_get)
    tr = tasks.add_parser("trials")
    tr.add_argument("id")
    tr.set_defaults(func=_cmd_trials)

    trials = sub.add_parser("trials").add_subparsers(dest="cmd", required=True)
    lg = trials.add_parser("logs")
    lg.add_argument("trial_id")
    lg.add_argument("--trajectory", action="store_true")
    lg.set_defaults(func=_cmd_logs)
    rs = trials.add_parser("result")
    rs.add_argument("trial_id")
    rs.set_defaults(func=_cmd_result)
    fls = trials.add_parser("files")
    fls.add_argument("trial_id")
    fls.add_argument("--prefix", default=None)
    fls.add_argument("--recursive", action="store_true")
    fls.set_defaults(func=_cmd_files)
    fl = trials.add_parser("file")
    fl.add_argument("trial_id")
    fl.add_argument("path")
    fl.set_defaults(func=_cmd_file)

    experiments = sub.add_parser("experiments").add_subparsers(dest="cmd", required=True)
    et = experiments.add_parser("trials")
    et.add_argument("exp_id")
    et.set_defaults(func=_cmd_experiment_trials)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
