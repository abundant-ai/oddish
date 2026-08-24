"""Read-only timing harness for bounded task-file tree pages.

Run against a preview or staging API with one small and one large existing
task. References use ``<task-id>@<version>``::

    ODDISH_PERF=1 \
    ODDISH_API_URL=https://preview-api.example \
    ODDISH_API_KEY=ok_... \
    ODDISH_PERF_TASK_FILES_SMALL=small-task-id@1 \
    ODDISH_PERF_TASK_FILES_LARGE=large-task-id@3 \
      pytest -s oddish/tests/perf/test_task_file_tree_timing_harness.py

The first request is reported as cold and the next four as warm. The harness
records latency, response bytes, returned rows, and upstream ``Server-Timing``
without creating or modifying tasks.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import asdict, dataclass

import httpx
import pytest

from oddish.cli.config import get_api_key

_SMALL_REF_ENV = "ODDISH_PERF_TASK_FILES_SMALL"
_LARGE_REF_ENV = "ODDISH_PERF_TASK_FILES_LARGE"
_WARM_SAMPLES = 4


@dataclass(frozen=True)
class FileTreeSample:
    scenario: str
    cache_state: str
    wall_ms: float
    status: int
    response_bytes: int
    file_rows: int
    dir_rows: int
    cursor_present: bool
    server_timing: str | None


def _parse_task_ref(raw: str) -> tuple[str, int]:
    task_id, separator, version_text = raw.rpartition("@")
    if not separator or not task_id or not version_text.isdigit():
        raise ValueError("expected <task-id>@<positive-version>")
    version = int(version_text)
    if version < 1:
        raise ValueError("version must be positive")
    return task_id, version


def _measure_page(
    client: httpx.Client,
    *,
    scenario: str,
    cache_state: str,
    task_id: str,
    version: int,
) -> FileTreeSample:
    started = time.perf_counter()
    response = client.get(
        f"/tasks/{task_id}/files",
        params={
            "recursive": "false",
            "inline": "false",
            "presign": "false",
            "limit": 100,
            "version": version,
        },
        headers={"Cache-Control": "no-cache"},
    )
    wall_ms = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    payload = response.json()
    files = payload.get("files") or []
    dirs = payload.get("dirs") or []

    assert len(files) + len(dirs) <= 100
    assert payload.get("recursive") is False
    assert all("content" not in row and "url" not in row for row in files)

    return FileTreeSample(
        scenario=scenario,
        cache_state=cache_state,
        wall_ms=round(wall_ms, 1),
        status=response.status_code,
        response_bytes=len(response.content),
        file_rows=len(files),
        dir_rows=len(dirs),
        cursor_present=bool(payload.get("cursor")),
        server_timing=response.headers.get("server-timing"),
    )


@pytest.mark.perf
def test_task_file_tree_small_and_large_cold_warm() -> None:
    configured = [
        ("task_file_tree_small", os.environ.get(_SMALL_REF_ENV, "").strip()),
        ("task_file_tree_large", os.environ.get(_LARGE_REF_ENV, "").strip()),
    ]
    missing = [name for name, raw in configured if not raw]
    if missing:
        pytest.skip(
            "task-file harness needs both existing task refs: set "
            f"{_SMALL_REF_ENV}=<id>@<version> and "
            f"{_LARGE_REF_ENV}=<id>@<version>"
        )

    api_url = os.environ["ODDISH_API_URL"].rstrip("/")
    api_key = get_api_key()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    samples: list[FileTreeSample] = []
    with httpx.Client(
        base_url=api_url,
        headers=headers,
        timeout=120,
        follow_redirects=True,
    ) as client:
        for scenario, raw in configured:
            task_id, version = _parse_task_ref(raw)
            samples.append(
                _measure_page(
                    client,
                    scenario=scenario,
                    cache_state="cold",
                    task_id=task_id,
                    version=version,
                )
            )
            for _ in range(_WARM_SAMPLES):
                samples.append(
                    _measure_page(
                        client,
                        scenario=scenario,
                        cache_state="warm",
                        task_id=task_id,
                        version=version,
                    )
                )

    summaries = []
    for scenario, _ in configured:
        scenario_samples = [s for s in samples if s.scenario == scenario]
        cold = next(s for s in scenario_samples if s.cache_state == "cold")
        warm_ms = [s.wall_ms for s in scenario_samples if s.cache_state == "warm"]
        summaries.append(
            {
                "scenario": scenario,
                "cold_ms": cold.wall_ms,
                "warm_median_ms": round(statistics.median(warm_ms), 1),
                "warm_max_ms": round(max(warm_ms), 1),
                "response_bytes": cold.response_bytes,
                "root_rows": cold.file_rows + cold.dir_rows,
            }
        )

    print(
        json.dumps(
            {
                "samples": [asdict(sample) for sample in samples],
                "summary": summaries,
            },
            indent=2,
            sort_keys=True,
        )
    )
