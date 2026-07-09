import asyncio
import base64
import binascii
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from oddish.config import settings
from oddish.db import TrialEventModel, TrialModel
from oddish.db.connection import get_session
from oddish.model_pricing import estimate_cost_usd
from oddish.workers.queue.shared import console

MAX_CHUNK_BYTES = 256 * 1024
EXEC_TIMEOUT_SEC = 30
MAX_CONSECUTIVE_FAILURES = 5
MAX_TRIAL_EVENTS = 5000
PAYLOAD_CLIP_CHARS = 2048


def split_lines(buf: bytes) -> tuple[list[bytes], bytes]:
    *lines, rest = buf.split(b"\n")
    return lines, rest


@dataclass
class UsageTotals:
    input_tokens: int = 0
    cache_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    model: str | None = None


class Fold(Protocol):
    def feed_line(self, line: bytes) -> list[dict[str, Any]]: ...

    def totals(self) -> UsageTotals: ...

    def on_truncate(self) -> None:
        """Called when the tailed log shrinks (a new session re-teed the file)."""
        ...

    @property
    def has_usage(self) -> bool: ...


def _parse_json_line(line: bytes) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith(b"{"):
        return None
    try:
        event = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return event if isinstance(event, dict) else None


def _clipped_payload(key: str, value: Any, **extra: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        value = json.dumps(value, default=str)
    payload: dict[str, Any] = {key: value[:PAYLOAD_CLIP_CHARS], **extra}
    if len(value) > PAYLOAD_CLIP_CHARS:
        payload["truncated"] = True
    return payload


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict)
        )
    return "" if content is None else str(content)


def _render_assistant_blocks(message: dict) -> list[dict[str, Any]]:
    content = message.get("content")
    rendered: list[dict[str, Any]] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            rendered.append(
                {"kind": "message", "payload": _clipped_payload("text", block["text"])}
            )
        elif block.get("type") == "tool_use":
            rendered.append(
                {
                    "kind": "tool_use",
                    "payload": _clipped_payload(
                        "input",
                        block.get("input") or {},
                        name=str(block.get("name") or ""),
                    ),
                }
            )
    return rendered


def _render_tool_results(message: Any) -> list[dict[str, Any]]:
    content = message.get("content") if isinstance(message, dict) else None
    rendered: list[dict[str, Any]] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        payload = _clipped_payload("content", _tool_result_text(block.get("content")))
        if block.get("is_error"):
            payload["is_error"] = True
        rendered.append({"kind": "tool_result", "payload": payload})
    return rendered


@dataclass
class ClaudeUsageFold:
    usage_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    model: str | None = None

    @property
    def has_usage(self) -> bool:
        return bool(self.usage_by_id)

    def on_truncate(self) -> None:
        # Usage keys off unique per-message ids, so a re-teed log's fresh ids
        # accumulate into usage_by_id on their own — no banking needed.
        return None

    def feed_line(self, line: bytes) -> list[dict[str, Any]]:
        event = _parse_json_line(line)
        if event is None:
            return []
        if event.get("type") == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                return []
            msg_id = message.get("id")
            usage = message.get("usage")
            if msg_id and isinstance(usage, dict):
                self.usage_by_id[msg_id] = usage
                model = message.get("model")
                if isinstance(model, str) and model:
                    self.model = model
            return _render_assistant_blocks(message)
        if event.get("type") == "user":
            return _render_tool_results(event.get("message"))
        if event.get("type") == "result":
            value = event.get("result") or event.get("subtype") or ""
            return [{"kind": "summary", "payload": _clipped_payload("text", value)}]
        return []

    def totals(self) -> UsageTotals:
        t = UsageTotals(model=self.model)
        for usage in self.usage_by_id.values():
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            cache_write = int(usage.get("cache_creation_input_tokens") or 0)
            t.input_tokens += int(usage.get("input_tokens") or 0) + cache_read + cache_write
            t.cache_tokens += cache_read
            t.cache_write_tokens += cache_write
            t.output_tokens += int(usage.get("output_tokens") or 0)
        return t


def _render_codex_item(item: Any) -> list[dict[str, Any]]:
    if not isinstance(item, dict):
        return []
    item_type = item.get("type")
    if item_type in ("agent_message", "reasoning"):
        text = item.get("text")
        if not text:
            return []
        return [{"kind": "message", "payload": _clipped_payload("text", text)}]
    if item_type == "command_execution":
        result = _clipped_payload("content", item.get("aggregated_output") or "")
        exit_code = item.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            result["is_error"] = True
        return [
            {
                "kind": "tool_use",
                "payload": _clipped_payload(
                    "input",
                    {"command": str(item.get("command") or "")},
                    name="shell",
                ),
            },
            {"kind": "tool_result", "payload": result},
        ]
    return []


@dataclass
class CodexUsageFold:
    usage: dict[str, Any] | None = None
    model: str | None = None
    banked: UsageTotals = field(default_factory=UsageTotals)

    @property
    def has_usage(self) -> bool:
        return self.usage is not None or self.banked != UsageTotals()

    def on_truncate(self) -> None:
        # turn.completed usage is cumulative per codex session; a re-teed log
        # restarts that counter, so bank the last session's total before it is
        # overwritten (keep-last) by the new session's smaller running total.
        session = self._session_totals()
        self.banked.input_tokens += session.input_tokens
        self.banked.cache_tokens += session.cache_tokens
        self.banked.output_tokens += session.output_tokens
        self.usage = None

    def feed_line(self, line: bytes) -> list[dict[str, Any]]:
        event = _parse_json_line(line)
        if event is None:
            return []
        event_type = event.get("type")
        if event_type == "item.completed":
            return _render_codex_item(event.get("item"))
        if event_type == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.usage = usage
            return []
        if event_type == "turn.failed":
            error = event.get("error")
            message = (
                error.get("message") if isinstance(error, dict) else event.get("message")
            )
            if not message:
                return []
            return [{"kind": "summary", "payload": _clipped_payload("text", message)}]
        return []

    def _session_totals(self) -> UsageTotals:
        usage = self.usage or {}
        return UsageTotals(
            input_tokens=int(usage.get("input_tokens") or 0),
            cache_tokens=int(usage.get("cached_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )

    def totals(self) -> UsageTotals:
        session = self._session_totals()
        return UsageTotals(
            input_tokens=self.banked.input_tokens + session.input_tokens,
            cache_tokens=self.banked.cache_tokens + session.cache_tokens,
            output_tokens=self.banked.output_tokens + session.output_tokens,
            model=self.model,
        )


@dataclass(frozen=True)
class Adapter:
    matches: Callable[[str], bool]
    log_path: str
    make_fold: Callable[[str | None], Fold]


ADAPTERS: tuple[Adapter, ...] = (
    Adapter(
        matches=lambda agent: "claude-code" in agent,
        log_path="/logs/agent/claude-code.txt",
        make_fold=lambda model: ClaudeUsageFold(model=model),
    ),
    Adapter(
        matches=lambda agent: "codex" in agent,
        log_path="/logs/agent/codex.txt",
        make_fold=lambda model: CodexUsageFold(model=model),
    ),
)


def _adapter_for(agent: str | None) -> Adapter | None:
    name = (agent or "").strip().lower()
    if not name:
        return None
    return next((a for a in ADAPTERS if a.matches(name)), None)


def supports(agent: str | None) -> bool:
    return _adapter_for(agent) is not None


def price_totals(totals: UsageTotals) -> float | None:
    model = totals.model
    if not model:
        return None
    try:
        return estimate_cost_usd(
            model,
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
            cached_tokens=totals.cache_tokens,
            cache_write_tokens=totals.cache_write_tokens,
        )
    except Exception:
        return None


class LiveTailer:
    def __init__(
        self,
        *,
        trial_id: str,
        environment: Any,
        attempt: int,
        log_path: str,
        fold: Fold,
    ):
        self.trial_id = trial_id
        self.environment = environment
        self.attempt = attempt
        self.log_path = log_path
        self.fold = fold
        self.offset = 0
        self.carry = b""
        self.seq = 0
        self.capped = False
        self.pending_events: list[dict[str, Any]] = []
        self._stop = asyncio.Event()
        self._last_written: tuple | None = None
        self._last_cost: float | None = None
        self.replaced = False

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        try:
            await self._run_loop()
        finally:
            if self.carry and not self.replaced:
                with contextlib.suppress(Exception):
                    self._buffer_events(self.fold.feed_line(self.carry))
            with contextlib.suppress(Exception):
                await asyncio.shield(self._flush_events())
            with contextlib.suppress(Exception):
                await asyncio.shield(self._checkpoint())

    async def _run_loop(self) -> None:
        failures = 0
        while True:
            stopping = self._stop.is_set()
            try:
                await self._tick()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    console.print(
                        f"[dim]Trial {self.trial_id} live tail disabled "
                        f"after {failures} failures: {exc}[/dim]"
                    )
                    return
            if stopping:
                return
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=settings.live_tail_interval_sec
                )
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        command = (
            f"set -o pipefail; wc -c < '{self.log_path}' 2>/dev/null || echo -1; "
            f"tail -c +{self.offset + 1} '{self.log_path}'"
            f" 2>/dev/null | head -c {MAX_CHUNK_BYTES} | base64 | tr -d '\\n'"
        )
        result = await self.environment.exec(
            command=command, timeout_sec=EXEC_TIMEOUT_SEC
        )
        return_code = getattr(result, "return_code", 0)
        if return_code not in (0, None):
            if return_code == 127 or self.offset > 0:
                raise RuntimeError(f"tail exec failed rc={return_code}")
            return
        stdout = (result.stdout or "").strip()
        size_line, _, encoded = stdout.partition("\n")
        try:
            size = int(size_line.strip())
        except ValueError:
            size, encoded = None, stdout
        if size is not None and 0 <= size < self.offset:
            self.fold.on_truncate()
            self.offset = 0
            self.carry = b""
            return
        encoded = encoded.strip()
        if encoded:
            try:
                raw = base64.b64decode(encoded, validate=True)
            except binascii.Error as exc:
                raise RuntimeError(f"invalid base64 tail output: {exc}") from exc
            lines, self.carry = split_lines(self.carry + raw)
            self.offset += len(raw)
            for line in lines:
                self._buffer_events(self.fold.feed_line(line))
        await self._flush_events()
        await self._checkpoint()

    def _buffer_events(self, rendered: list[dict[str, Any]]) -> None:
        if self.replaced or self.capped:
            return
        for event in rendered:
            self.seq += 1
            if self.seq > MAX_TRIAL_EVENTS:
                self.pending_events.append(
                    {
                        "seq": self.seq,
                        "kind": "summary",
                        "payload": _clipped_payload(
                            "text",
                            f"Transcript capped at {MAX_TRIAL_EVENTS} events; "
                            "further output omitted.",
                            capped=True,
                        ),
                    }
                )
                self.capped = True
                return
            self.pending_events.append({"seq": self.seq, **event})

    async def _flush_events(self) -> None:
        if not self.pending_events or self.replaced:
            return
        rows = [
            {"trial_id": self.trial_id, "attempt": self.attempt, **event}
            for event in self.pending_events
        ]
        stmt = pg_insert(TrialEventModel).values(rows).on_conflict_do_nothing()
        async with get_session() as session:
            await session.execute(stmt)
        self.pending_events.clear()

    async def _checkpoint(self) -> None:
        if not self.fold.has_usage:
            return
        totals = self.fold.totals()
        cost = price_totals(totals)
        if cost is None:
            cost = self._last_cost
        elif self._last_cost is not None:
            cost = max(cost, self._last_cost)
        values: dict[str, Any] = {
            "input_tokens": totals.input_tokens,
            "cache_tokens": totals.cache_tokens,
            "cache_write_tokens": totals.cache_write_tokens,
            "output_tokens": totals.output_tokens,
            "cost_usd": cost,
        }
        state = tuple(values.values())
        if state == self._last_written or self.replaced:
            return
        async with get_session() as session:
            result = await session.execute(
                update(TrialModel)
                .where(
                    TrialModel.id == self.trial_id,
                    TrialModel.finished_at.is_(None),
                )
                .values(**values)
            )
        if getattr(result, "rowcount", None) == 0:
            self.request_stop()
            return
        self._last_written = state
        self._last_cost = cost


_tailers: dict[str, tuple[LiveTailer, asyncio.Task]] = {}


def start(
    *,
    trial_id: str,
    environment: Any,
    attempt: int,
    agent: str | None,
    model: str | None,
) -> None:
    if not settings.live_tail_enabled:
        return
    adapter = _adapter_for(agent)
    if environment is None or adapter is None:
        return
    tailer = LiveTailer(
        trial_id=trial_id,
        environment=environment,
        attempt=attempt,
        log_path=adapter.log_path,
        fold=adapter.make_fold(model),
    )
    old = _tailers.pop(trial_id, None)
    if old:
        old_tailer, old_task = old
        old_tailer.replaced = True
        old_tailer.request_stop()
        old_task.cancel()
        tailer._last_cost = old_tailer._last_cost
        tailer.seq = old_tailer.seq
        tailer.capped = old_tailer.capped
        tailer.pending_events = list(old_tailer.pending_events)
        if old_tailer.attempt == attempt:
            tailer.offset = old_tailer.offset
            tailer.carry = old_tailer.carry
            tailer.fold = old_tailer.fold
    _tailers[trial_id] = (tailer, asyncio.create_task(tailer.run()))


def request_stop(trial_id: str) -> None:
    entry = _tailers.get(trial_id)
    if entry:
        entry[0].request_stop()


async def shutdown(trial_id: str, timeout_sec: float = 15.0) -> int | None:
    entry = _tailers.pop(trial_id, None)
    if not entry:
        return None
    tailer, task = entry
    tailer.request_stop()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_sec)
    except asyncio.TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), timeout=5
            )
    except Exception:
        pass
    return tailer.attempt


async def purge_events(trial_id: str, attempt: int | None) -> None:
    if attempt is None:
        return
    try:
        async with get_session() as session:
            await session.execute(
                delete(TrialEventModel).where(
                    TrialEventModel.trial_id == trial_id,
                    TrialEventModel.attempt <= attempt,
                )
            )
    except Exception as exc:
        console.print(f"[dim]Trial {trial_id} event purge failed: {exc}[/dim]")
