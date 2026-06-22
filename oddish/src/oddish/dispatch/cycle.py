"""The host-agnostic dispatch loop (design spec §5.2).

The **core** owns discovery, spawn-plan math, per-poll caps, the pre-spawn
admission gate, and the wake trigger; a ``Dispatcher`` only runs / observes /
cancels workers. Modal's ``poll_queue`` cron and the standalone server both
drive ``run_dispatch_cycle`` with their own ``Dispatcher`` — so the scheduling
brain is identical everywhere and only the fan-out primitive differs.

Trigger: an **event-driven wake** on enqueue / slot-release, with the timed
poll demoted to a *fallback* (still required for ``available_after`` retries).
The in-app signal works whenever the API and dispatcher share a process (the
standalone server, the Docker stack); on Modal they are separate containers, so
the signal is a no-op there and the cron/fallback poll remains — exactly the
documented interim that sidesteps the Supavisor LISTEN/NOTIFY caveat.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Collection, Sequence

from oddish.dispatch.ports import Dispatcher, WorkerHandle
from oddish.workers.queue.worker_job_dispatcher import (
    build_spawn_plan,
    discover_active_worker_job_queue_keys,
    fetch_running_worker_handles,
    get_worker_job_org_queue_counts,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Admission:
    """Result of the pre-spawn admission check for one ``queue_key``."""

    admitted: bool
    reason: str = ""


AdmissionCheck = Callable[[str], Admission]


def admit_all(queue_key: str) -> Admission:
    """Default admission: admit every queue_key (capability/ref checks plug in)."""
    return Admission(admitted=True)


def admit_spawn_plan(
    spawn_plan: Sequence[str], admit: AdmissionCheck
) -> tuple[list[str], dict[str, str]]:
    """Filter a spawn plan through the admission gate.

    Returns the admitted plan (preserving order/multiplicity) and a mapping of
    rejected ``queue_key`` → human-readable reason (the §12 why-waiting field).
    """
    admitted: list[str] = []
    rejected: dict[str, str] = {}
    for queue_key in spawn_plan:
        if queue_key in rejected:
            continue
        decision = admit(queue_key)
        if decision.admitted:
            admitted.append(queue_key)
        else:
            rejected[queue_key] = decision.reason
    return admitted, rejected


def compute_why_waiting(
    *,
    queued_by_queue: dict[str, int],
    running_by_queue: dict[str, int],
    concurrency_limits: dict[str, int],
    spawned_keys: Collection[str],
    max_workers: int,
    base_reasons: dict[str, str] | None = None,
) -> dict[str, str]:
    """Name a reason for every queue_key with queued work that got no spawn.

    Starts from ``base_reasons`` (admission rejections) and adds a capacity
    reason for the rest — over-cap (no free slot) or per-poll budget exhausted.
    The §12 why-waiting field; shared by ``run_dispatch_cycle`` and the Modal
    ``poll_queue`` so both hosts stamp the same reasons.
    """
    why_waiting: dict[str, str] = dict(base_reasons or {})
    spawned = set(spawned_keys)
    for queue_key, queued in queued_by_queue.items():
        if queued <= 0 or queue_key in spawned or queue_key in why_waiting:
            continue
        limit = concurrency_limits.get(queue_key, 0)
        running = running_by_queue.get(queue_key, 0)
        if limit - running <= 0:
            why_waiting[queue_key] = (
                f"waiting for slot (limit {limit}, running {running})"
            )
        else:
            why_waiting[queue_key] = f"spawn cap reached (max {max_workers})"
    return why_waiting


@dataclass(frozen=True)
class DispatchCycleResult:
    """Outcome of one dispatch tick — the transparency record (§12)."""

    queue_keys: tuple[str, ...]
    spawn_plan: list[str]
    admitted: list[str]
    handles: list[WorkerHandle]
    why_waiting: dict[str, str] = field(default_factory=dict)
    queued_total: int = 0
    running_total: int = 0


DiscoverFn = Callable[[], Awaitable[Sequence[str]]]
CountsFn = Callable[
    [Sequence[str]],
    Awaitable[tuple[dict[tuple[str | None, str], int], dict[str, int]]],
]


async def run_dispatch_cycle(
    dispatcher: Dispatcher,
    *,
    max_workers: int,
    concurrency_for: Callable[[str], int],
    admit: AdmissionCheck = admit_all,
    on_stage: Callable[[list[str], dict[str, str]], Awaitable[None]] | None = None,
    _discover: DiscoverFn = discover_active_worker_job_queue_keys,
    _counts: CountsFn = get_worker_job_org_queue_counts,
) -> DispatchCycleResult:
    """Run one dispatch tick: discover → plan → admit → spawn.

    Pure scheduling lives here; the ``Dispatcher`` only fans out the admitted
    plan. Every still-waiting queue_key gets a named reason in ``why_waiting``.
    ``on_stage(admitted, why_waiting)`` is an optional hook (the host wires it to
    ``stamp_dispatch_stage``) that records the §12 per-stage observability;
    omitted in pure/unit contexts so the cycle stays DB-free.
    """
    queue_keys = tuple(await _discover())
    queued_by_org_queue, running_by_queue = await _counts(queue_keys)
    concurrency_limits = {qk: concurrency_for(qk) for qk in queue_keys}

    spawn_plan = build_spawn_plan(
        queued_by_org_queue=queued_by_org_queue,
        running_by_queue=running_by_queue,
        concurrency_limits=concurrency_limits,
        max_workers=max_workers,
    )

    admitted, rejected = admit_spawn_plan(spawn_plan, admit)

    queued_by_queue: dict[str, int] = {}
    for (_org, queue_key), queued in queued_by_org_queue.items():
        queued_by_queue[queue_key] = queued_by_queue.get(queue_key, 0) + queued

    why_waiting = compute_why_waiting(
        queued_by_queue=queued_by_queue,
        running_by_queue=running_by_queue,
        concurrency_limits=concurrency_limits,
        spawned_keys=set(admitted),
        max_workers=max_workers,
        base_reasons=rejected,
    )

    handles = list(await dispatcher.spawn(spawn_plan=admitted))

    if on_stage is not None:
        # Telemetry is best-effort: a stamping failure must never break dispatch.
        try:
            await on_stage(admitted, why_waiting)
        except Exception:
            pass

    return DispatchCycleResult(
        queue_keys=queue_keys,
        spawn_plan=spawn_plan,
        admitted=admitted,
        handles=handles,
        why_waiting=why_waiting,
        queued_total=sum(queued_by_queue.values()),
        running_total=sum(running_by_queue.values()),
    )


FetchHandlesFn = Callable[[], Awaitable[list[tuple[str, str]]]]


async def reattach_in_flight_workers(
    dispatcher: Dispatcher,
    *,
    _fetch: FetchHandlesFn = fetch_running_worker_handles,
) -> list[WorkerHandle]:
    """Reattach durable in-flight worker handles after a control-plane restart.

    Reads the persisted handles of RUNNING ``worker_jobs`` and calls
    ``Dispatcher.recover`` for each (the Nomad ``RecoverTask`` step), so a
    restarted dispatcher reacquires its in-flight workers instead of orphaning
    them. Backends whose handles are not durable return ``None`` from
    ``recover`` and are skipped (lease expiry + re-claim covers them, §14.3).
    """
    recovered: list[WorkerHandle] = []
    for queue_key, handle_id in await _fetch():
        handle = await dispatcher.recover(
            WorkerHandle(
                provider=dispatcher.name,
                queue_key=queue_key,
                id=handle_id,
                provisional=False,
            ).serialize()
        )
        if handle is not None:
            recovered.append(handle)
    return recovered


async def reclaim_leaked_workers(
    dispatcher: Dispatcher, *, alive: Collection[str]
) -> int:
    """Cancel managed workers with no live ``worker_jobs`` row (tag-based GC).

    For scalers that expose ``list_managed`` (Docker, Kubernetes), list every
    worker this dispatcher manages and cancel the ones whose id is not in
    ``alive`` (the set of worker handle ids still backing a live row). A no-op
    for scalers without ``list_managed`` (in-process / Modal). The container/Job
    runtimes also self-reclaim (``--rm`` / ``ttlSecondsAfterFinished``), so this
    is a backstop for the leak case (CRI/Nomad list-by-tag pattern, §6.5).
    """
    list_managed = getattr(dispatcher, "list_managed", None)
    if list_managed is None:
        return 0
    managed = await list_managed()
    alive_set = set(alive)
    leaked = [h for h in managed if h.id and h.id not in alive_set]
    if not leaked:
        return 0
    return await dispatcher.cancel(leaked)


class DispatchTrigger:
    """Event-driven wake with a timed fallback.

    ``signal()`` wakes a pending ``wait()`` immediately (enqueue / slot-release);
    otherwise ``wait()`` returns after ``fallback_interval`` seconds so
    ``available_after`` retries are still picked up.
    """

    def __init__(self, *, fallback_interval: float) -> None:
        self._event = asyncio.Event()
        self._fallback_interval = fallback_interval

    @property
    def fallback_interval(self) -> float:
        return self._fallback_interval

    def signal(self) -> None:
        self._event.set()

    async def wait(self) -> str:
        try:
            await asyncio.wait_for(
                self._event.wait(), timeout=self._fallback_interval
            )
            self._event.clear()
            return "signal"
        except asyncio.TimeoutError:
            return "fallback"


_TRIGGER: DispatchTrigger | None = None


def get_dispatch_trigger(*, fallback_interval: float = 20.0) -> DispatchTrigger:
    """Return the process-wide dispatch trigger (created on first use)."""
    global _TRIGGER
    if _TRIGGER is None:
        _TRIGGER = DispatchTrigger(fallback_interval=fallback_interval)
    return _TRIGGER


def signal_dispatch() -> None:
    """Wake the dispatch loop if one is running in this process (in-app signal).

    A no-op when no loop has registered a trigger (e.g. on Modal, where the API
    and dispatcher are separate containers and the cron/fallback poll drives
    dispatch instead).
    """
    if _TRIGGER is not None:
        _TRIGGER.signal()


async def run_dispatch_loop(
    dispatcher: Dispatcher,
    *,
    max_workers: int,
    concurrency_for: Callable[[str], int],
    admit: AdmissionCheck = admit_all,
    on_stage: Callable[[list[str], dict[str, str]], Awaitable[None]] | None = None,
    fallback_interval: float = 20.0,
    _stop: Callable[[], bool] = lambda: False,
) -> None:
    """Drive ``run_dispatch_cycle`` forever, woken by the trigger or fallback."""
    trigger = get_dispatch_trigger(fallback_interval=fallback_interval)

    # Control-plane restart: reattach in-flight workers before the first tick so
    # a restarted dispatcher reacquires (not orphans) them. Best-effort.
    try:
        recovered = await reattach_in_flight_workers(dispatcher)
        if recovered:
            logger.info(
                "reattached %d in-flight worker(s) on startup", len(recovered)
            )
    except Exception as exc:  # noqa: BLE001 - reattach must not block startup
        logger.warning("startup reattach skipped: %r", exc)

    while not _stop():
        await run_dispatch_cycle(
            dispatcher,
            max_workers=max_workers,
            concurrency_for=concurrency_for,
            admit=admit,
            on_stage=on_stage,
        )
        await trigger.wait()
