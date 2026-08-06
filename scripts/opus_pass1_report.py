# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib"]
# ///
"""Per-task pass@1 plot for one model family across sample collections.

The ByteDance sample experiments are *collection* experiments: they pin a task
set (and a task version per task) but only gather a subset of the trials that
exist for those tasks. This script therefore takes each collection as the task
roster, then pulls ``/tasks/{id}/detail`` (all trials, combine copies already
excluded server-side) and scores every trial whose model matches
``--model-substring`` on the collection's pinned task version.

pass@1 follows the dashboard convention (frontend ``getPassAtOneValue``):
passing = reward == 1; failed and skipped trials stay in the denominator;
probes and superseded retries are excluded.

Run with uv (dependencies resolve from the inline metadata above):

    ODDISH_API_KEY=... uv run scripts/opus_pass1_report.py \
        --out-png opus-pass1.png --out-csv opus-pass1.csv

Defaults target the 2026-08-04 ByteDance sample collections and Opus.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

DEFAULT_API_URL = os.environ.get("ODDISH_API_URL", "https://abundant-ai--api.modal.run")
DEFAULT_COLLECTIONS = [
    ("bc9dcf81", "MLE"),
    ("7fac77d1", "E2E migration"),
    ("a32324bc", "Binary decomp"),
    ("ffe287bd", "Formal verification"),
]
TERMINAL_STATUSES = {"success", "failed", "skipped"}

# Light-mode chart tokens (dataviz reference palette).
PAGE = "#f9f9f7"
SURFACE = "#fcfcfb"
SERIES = "#2a78d6"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"


def _get(api_url: str, path: str, cache_dir: Path | None, cache_key: str) -> object:
    if cache_dir is not None:
        cached = cache_dir / cache_key
        if cached.exists() and cached.stat().st_size > 0:
            return json.loads(cached.read_text())
    key = os.environ.get("ODDISH_API_KEY")
    if not key:
        sys.exit("Set ODDISH_API_KEY (create one at https://www.oddish.app).")
    req = urllib.request.Request(
        f"{api_url}{path}", headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / cache_key).write_bytes(raw)
    return json.loads(raw)


@dataclass
class TaskRow:
    task_id: str
    name: str
    pinned_version_id: str | None
    n: int = 0
    passes: int = 0
    agents: Counter = field(default_factory=Counter)
    models: Counter = field(default_factory=Counter)

    @property
    def pass_at_1(self) -> float | None:
        return self.passes / self.n if self.n else None


def collect(
    api_url: str,
    collections: list[tuple[str, str]],
    model_substring: str,
    cache_dir: Path | None,
) -> dict[str, list[TaskRow]]:
    rosters: dict[str, list[TaskRow]] = {}
    needed: dict[str, None] = {}
    for exp_id, label in collections:
        tasks = _get(
            api_url,
            f"/tasks?experiment_id={exp_id}&include_trials=true&compact_trials=true",
            cache_dir,
            f"collection_{exp_id}.json",
        )
        rows = [
            TaskRow(
                task_id=t["id"],
                name=t.get("name") or t["id"],
                # The version whose trials the collection displays; falls back
                # to the task's current version for collections that never
                # pinned one.
                pinned_version_id=t.get("trial_version_id")
                or t.get("current_version_id"),
            )
            for t in tasks
        ]
        rosters[label] = rows
        for row in rows:
            needed[row.task_id] = None

    def fetch_detail(task_id: str) -> tuple[str, object]:
        return task_id, _get(
            api_url, f"/tasks/{task_id}/detail", cache_dir, f"detail_{task_id}.json"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        details = dict(pool.map(fetch_detail, needed))

    sub = model_substring.lower()
    for rows in rosters.values():
        for row in rows:
            detail = details[row.task_id]
            task = detail.get("task") if isinstance(detail, dict) else None
            trials = (task or detail).get("trials") or []
            for trial in trials:
                if sub not in (trial.get("model") or "").lower():
                    continue
                if trial.get("is_probe") or trial.get("superseded_by_trial_id"):
                    continue
                if trial.get("task_version_id") != row.pinned_version_id:
                    continue
                if trial.get("status") not in TERMINAL_STATUSES:
                    continue
                row.n += 1
                row.passes += 1 if trial.get("reward") == 1 else 0
                row.agents[trial.get("agent") or "?"] += 1
                row.models[trial.get("model") or "?"] += 1
    return rosters


def display_name(row: TaskRow, taken: set[str]) -> str:
    parts = row.name.rsplit("-", 1)
    if len(parts) == 2 and len(parts[1]) == 8 and all(
        c in "0123456789abcdef" for c in parts[1]
    ):
        short = parts[0]
        if short not in taken:
            taken.add(short)
            return short
    taken.add(row.name)
    return row.name


def rounded_bar(ax, width: float, y_center: float, height: float, radius_inch: float):
    """Bar with a rounded data-end and a square baseline end."""
    import matplotlib.patches as mpatches
    from matplotlib.path import Path as MplPath

    if width <= 0:
        return
    to_px = ax.transData.transform
    (x0, ya) = to_px((0.0, y_center - height / 2))
    (x1, yb) = to_px((width, y_center + height / 2))
    left, right = min(x0, x1), max(x0, x1)
    bottom, top = min(ya, yb), max(ya, yb)
    r = min(radius_inch * ax.figure.dpi, (right - left), (top - bottom) / 2)
    k = 0.5523 * r  # quarter-circle Bezier constant
    verts_px = [
        (left, bottom),
        (right - r, bottom),
        (right - r + k, bottom), (right, bottom + r - k), (right, bottom + r),
        (right, top - r),
        (right, top - r + k), (right - r + k, top), (right - r, top),
        (left, top),
        (left, bottom),
    ]
    codes = [
        MplPath.MOVETO,
        MplPath.LINETO,
        MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4,
        MplPath.LINETO,
        MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4,
        MplPath.LINETO,
        MplPath.CLOSEPOLY,
    ]
    verts = ax.transData.inverted().transform(verts_px)
    ax.add_patch(
        mpatches.PathPatch(
            MplPath(verts, codes), facecolor=SERIES, edgecolor="none", zorder=3
        )
    )


def render(
    rosters: dict[str, list[TaskRow]],
    collections: list[tuple[str, str]],
    out_png: Path,
    model_label: str,
) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.transforms import blended_transform_factory

    labels = [label for _, label in collections]
    ids_by_label = {label: exp_id for exp_id, label in collections}
    columns = [[labels[0]], labels[1:]] if len(labels) > 2 else [labels]

    row_h, title_h, gap_h = 0.212, 0.60, 0.30
    top_h, footer_h, margin = 1.05, 0.62, 0.22

    def column_height(col: list[str]) -> float:
        rows = sum(len(rosters[label]) for label in col)
        return rows * row_h + len(col) * title_h + (len(col) - 1) * gap_h

    body_h = max(column_height(col) for col in columns)
    fig_h = top_h + body_h + footer_h + margin
    gutter, plot_w, mid_gap, edge = 2.18, 4.75, 0.72, 0.30
    fig_w = edge + len(columns) * (gutter + plot_w) + (len(columns) - 1) * mid_gap + edge

    fig = plt.figure(figsize=(fig_w, fig_h), dpi=200, facecolor=PAGE)

    def add_panel(label: str, x_in: float, y_top_in: float) -> float:
        rows = sorted(
            rosters[label],
            key=lambda r: (r.pass_at_1 is None, -(r.pass_at_1 or 0), -r.n, r.task_id),
        )
        n = len(rows)
        h_in = n * row_h
        ax = fig.add_axes([
            (x_in + gutter) / fig_w,
            (fig_h - y_top_in - title_h - h_in) / fig_h,
            plot_w / fig_w,
            h_in / fig_h,
        ])
        ax.set_facecolor(SURFACE)
        ax.set_xlim(0, 1)
        ax.set_ylim(n - 0.5, -0.5)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.vlines([0.25, 0.5, 0.75, 1.0], -0.5, n - 0.5, color=GRID, lw=0.8, zorder=1)
        ax.axvline(0, color=BASELINE, lw=1.0, zorder=2)
        ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
        ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"])
        ax.tick_params(axis="x", length=0, labelsize=7, colors=MUTED)
        ax.set_yticks([])

        name_tr = blended_transform_factory(ax.transAxes, ax.transData)
        taken: set[str] = set()
        for i, row in enumerate(rows):
            scored = row.pass_at_1 is not None
            ax.text(
                -0.012, i, display_name(row, taken),
                transform=name_tr, ha="right", va="center", fontsize=7.6,
                color=INK if scored else MUTED,
                fontstyle="normal" if scored else "italic",
            )
            if scored:
                rounded_bar(ax, row.pass_at_1, i, 0.60, radius_inch=4 / 96)
                # Near the right edge there is no room outside the bar end, so
                # the count moves inside the fill (white clears the blue).
                inside = row.pass_at_1 > 0.92
                ax.annotate(
                    f"{row.passes}/{row.n}", xy=(row.pass_at_1, i),
                    xytext=(-5 if inside else 4, 0), textcoords="offset points",
                    ha="right" if inside else "left", va="center",
                    fontsize=6.6, color="#ffffff" if inside else MUTED,
                )
            else:
                ax.annotate(
                    "— not run", xy=(0, i), xytext=(4, 0),
                    textcoords="offset points", va="center",
                    fontsize=6.6, color=MUTED, fontstyle="italic",
                )

        scored_rows = [r for r in rows if r.pass_at_1 is not None]
        mean = (
            sum(r.pass_at_1 for r in scored_rows) / len(scored_rows)
            if scored_rows else 0.0
        )
        if scored_rows:
            ax.axvline(mean, color=INK_2, lw=1.0, zorder=2.5)
            # Label sits above the plot area so it never collides with the
            # top row's bar.
            mean_tr = blended_transform_factory(ax.transData, ax.transAxes)
            right_side = mean <= 0.8
            ax.text(
                mean + (0.012 if right_side else -0.012), 1.012, f"mean {mean:.0%}",
                transform=mean_tr, ha="left" if right_side else "right", va="bottom",
                fontsize=6.8, color=INK_2,
            )
        n_trials = sum(r.n for r in rows)
        # The title rides high so the band just above the axes stays free for
        # the mean label (left) and the roster stats (right).
        ax.set_title(
            f"{label}", loc="left", fontsize=11.5, fontweight="bold", color=INK, pad=24
        )
        ax.text(
            1.0, 1.0, (
                f"runs on {len(scored_rows)}/{n} tasks · {n_trials} trials"
                f" · {ids_by_label[label]}"
            ),
            transform=ax.transAxes, ha="right", va="bottom", fontsize=7.3,
            color=INK_2,
        )
        return title_h + h_in + gap_h

    x_in = edge
    for col in columns:
        y_top_in = top_h
        for label in col:
            y_top_in += add_panel(label, x_in, y_top_in)
        x_in += gutter + plot_w + mid_gap

    fig.text(
        edge / fig_w, 1 - 0.30 / fig_h,
        f"{model_label} — pass@1 on ByteDance sample tasks",
        ha="left", va="top", fontsize=15, fontweight="bold", color=INK,
    )
    fig.text(
        edge / fig_w, 1 - 0.62 / fig_h,
        "Per-task pass@1 in the four sample categories; task sets and task "
        "versions as pinned by each sample collection.",
        ha="left", va="top", fontsize=9, color=INK_2,
    )
    footer = (
        "pass@1 = trials with reward 1 ÷ completed matching trials on the pinned task version"
        " · failed/skipped trials count as non-pass · probes and superseded retries excluded"
        " · category mean averages tasks with ≥1 run\n"
        f"{model_label.lower()} trials via claude-code, mini-swe-agent and terminus-2"
        " harnesses (models anthropic-hdo/claude-opus-4-8, global.anthropic.claude-opus-4-8)"
        " · “not run” = no matching trials on the sample version"
        f" · source: {DEFAULT_API_URL.split('//')[-1]} · {date.today().isoformat()}"
    )
    fig.text(
        edge / fig_w, 0.30 / fig_h, footer,
        ha="left", va="bottom", fontsize=6.9, color=MUTED, linespacing=1.65,
    )
    fig.savefig(out_png, dpi=200, facecolor=PAGE)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--experiment", action="append", metavar="ID=LABEL",
        help="Sample collection to include (repeatable); default: the four "
        "2026-08-04 ByteDance collections.",
    )
    parser.add_argument("--model-substring", default="opus")
    parser.add_argument("--model-label", default="Claude Opus 4.8")
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--cache-dir", type=Path, default=None,
                        help="Directory for cached API responses (optional).")
    parser.add_argument("--out-png", type=Path, default=Path("opus-pass1.png"))
    parser.add_argument("--out-csv", type=Path, default=None)
    args = parser.parse_args()

    collections = DEFAULT_COLLECTIONS
    if args.experiment:
        collections = []
        for spec in args.experiment:
            exp_id, _, label = spec.partition("=")
            collections.append((exp_id, label or exp_id))

    rosters = collect(args.api_url, collections, args.model_substring, args.cache_dir)

    for _, label in collections:
        rows = rosters[label]
        scored = [r for r in rows if r.pass_at_1 is not None]
        mean = sum(r.pass_at_1 for r in scored) / len(scored) if scored else 0.0
        print(
            f"{label}: mean pass@1 {mean:.1%} over {len(scored)}/{len(rows)} tasks "
            f"({sum(r.n for r in rows)} trials)"
        )

    if args.out_csv:
        with args.out_csv.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "category", "experiment_id", "task_id", "task_name",
                "pinned_version_id", "n_trials", "n_pass", "pass_at_1", "agents",
            ])
            for exp_id, label in collections:
                for row in rosters[label]:
                    writer.writerow([
                        label, exp_id, row.task_id, row.name,
                        row.pinned_version_id, row.n, row.passes,
                        "" if row.pass_at_1 is None else f"{row.pass_at_1:.4f}",
                        ";".join(f"{a}:{c}" for a, c in sorted(row.agents.items())),
                    ])
        print(f"wrote {args.out_csv}")

    render(rosters, collections, args.out_png, args.model_label)
    print(f"wrote {args.out_png}")


if __name__ == "__main__":
    main()
