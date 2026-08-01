"""Canonical dashboard addresses ("absolute addressability").

Every view in the dashboard is addressable by URL. These builders are the
Python-side source of that grammar, so the CLI, analyzers, and future
comment anchors mint links the frontend always resolves. Keep in sync with
the frontend's param handling (``frontend/src/lib/line-range.ts`` and the
drawer URL sync in ``task-detail-client`` / ``experiment-detail-view``).

Grammar (query params on ``/tasks/{task_id}`` or
``/experiments/{experiment_id}``):

- ``trial=<trial_id>``        open that trial's drawer
- ``drawer=task``             task-files drawer without a trial (task page)
- ``tab=<tab>``               trial pane tab (summary|live|files|trajectory|artifacts)
- ``file=<path>``             trial pane file — the files tab's browser,
                              or the artifact browser when ``tab=artifacts``
- ``lines=L<a>[-L<b>]``       trial pane line range (1-indexed, inclusive)
- ``taskFile=<path>``         task-definition pane file
- ``taskLines=L<a>[-L<b>]``   task-definition pane line range
- ``version=<id>``            task version row id, ``{task_id}-v{n}``
                              (task page only; bare numbers don't resolve)

Line ranges address *source lines* for every file type — code, logs, and
markdown alike (the rendered markdown view maps them onto blocks).
"""

from __future__ import annotations

import re
from urllib.parse import quote

_LINES_RE = re.compile(r"^L?(\d+)(?:-L?(\d+))?$", re.IGNORECASE)

LinesInput = str | int | tuple[int, int]


def format_lines(lines: LinesInput) -> str:
    """Normalize a line range to the canonical ``L<a>`` / ``L<a>-L<b>`` form.

    Accepts an int (single line), a ``(start, end)`` tuple, or a lenient
    string (``12``, ``12-20``, ``L12-L20``). Raises ``ValueError`` on
    malformed or non-positive input.
    """
    if isinstance(lines, int):
        start, end = lines, lines
    elif isinstance(lines, tuple):
        start, end = lines
    else:
        match = _LINES_RE.match(lines.strip())
        if not match:
            raise ValueError(f"invalid line range: {lines!r}")
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
    if start < 1 or end < 1:
        raise ValueError(f"line numbers are 1-indexed, got {start}-{end}")
    if end < start:
        start, end = end, start
    return f"L{start}" if start == end else f"L{start}-L{end}"


def trial_task_id(trial_id: str) -> str | None:
    """Derive the host task id from a trial id (``<task_id>-<n>``).

    Returns None when the id doesn't end in a numeric attempt suffix.
    """
    head, sep, tail = trial_id.rpartition("-")
    if not sep or not tail.isdigit() or not head:
        return None
    return head


def _version_id(task_id: str, version: int | str) -> str:
    """Normalize a version to the row id the dashboard resolves against.

    The task page matches ``?version=`` on version row ids
    (``{task_id}-v{n}``), never bare numbers — so ``3`` becomes
    ``{task_id}-v3`` and a full id passes through unchanged.
    """
    text = str(version).strip()
    return f"{task_id}-v{text}" if text.isdigit() else text


def _build(path: str, pairs: list[tuple[str, str]], base_url: str) -> str:
    query = "&".join(f"{key}={quote(value, safe='')}" for key, value in pairs)
    return f"{base_url.rstrip('/')}{path}" + (f"?{query}" if query else "")


def task_url(
    task_id: str,
    *,
    base_url: str = "",
    version: int | str | None = None,
    trial_id: str | None = None,
    drawer_task: bool = False,
    tab: str | None = None,
    file: str | None = None,
    lines: LinesInput | None = None,
    task_file: str | None = None,
    task_lines: LinesInput | None = None,
) -> str:
    """Canonical address on the task page (``/tasks/{task_id}``)."""
    if (tab or file or lines is not None) and not trial_id:
        raise ValueError("tab/file/lines address the trial pane; pass trial_id")
    if lines is not None and not file:
        raise ValueError("lines requires file")
    if file and tab not in (None, "files", "artifacts"):
        raise ValueError(
            "file/lines only resolve on the files or artifacts tab"
        )
    if task_lines is not None and not task_file:
        raise ValueError("task_lines requires task_file")
    pairs: list[tuple[str, str]] = []
    if version is not None:
        pairs.append(("version", _version_id(task_id, version)))
    if trial_id:
        pairs.append(("trial", trial_id))
    elif drawer_task or task_file:
        pairs.append(("drawer", "task"))
    if tab:
        pairs.append(("tab", tab))
    if file:
        pairs.append(("file", file))
    if lines is not None:
        pairs.append(("lines", format_lines(lines)))
    if task_file:
        pairs.append(("taskFile", task_file))
    if task_lines is not None:
        pairs.append(("taskLines", format_lines(task_lines)))
    return _build(f"/tasks/{quote(task_id, safe='')}", pairs, base_url)


def experiment_url(
    experiment_id: str,
    *,
    base_url: str = "",
    task_id: str | None = None,
    trial_id: str | None = None,
    tab: str | None = None,
    file: str | None = None,
    lines: LinesInput | None = None,
    task_file: str | None = None,
    task_lines: LinesInput | None = None,
) -> str:
    """Canonical address on the experiment page (``/experiments/{id}``).

    ``trial_id`` alone is a complete address — the frontend recovers the
    host task from the trial itself.
    """
    if (tab or file or lines is not None) and not trial_id:
        raise ValueError("tab/file/lines address the trial pane; pass trial_id")
    if lines is not None and not file:
        raise ValueError("lines requires file")
    if file and tab not in (None, "files", "artifacts"):
        raise ValueError(
            "file/lines only resolve on the files or artifacts tab"
        )
    if (task_file or task_lines is not None) and not (task_id or trial_id):
        raise ValueError("task_file/task_lines need a task_id or trial_id")
    if task_lines is not None and not task_file:
        raise ValueError("task_lines requires task_file")
    pairs: list[tuple[str, str]] = []
    if task_id:
        pairs.append(("task", task_id))
    if trial_id:
        pairs.append(("trial", trial_id))
    if tab:
        pairs.append(("tab", tab))
    if file:
        pairs.append(("file", file))
    if lines is not None:
        pairs.append(("lines", format_lines(lines)))
    if task_file:
        pairs.append(("taskFile", task_file))
    if task_lines is not None:
        pairs.append(("taskLines", format_lines(task_lines)))
    return _build(f"/experiments/{quote(experiment_id, safe='')}", pairs, base_url)
