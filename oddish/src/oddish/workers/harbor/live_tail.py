import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from oddish.config import settings
from oddish.db import TrialEventModel, TrialModel
from oddish.db.connection import get_session
from oddish.model_pricing import estimate_cost_usd
from oddish.transcript_safety import sanitize_transcript_text, sanitize_transcript_value
from oddish.workers.queue.shared import console

# Keep the same ~51 KiB/s drain capacity as the original 256 KiB / 5s
# cadence while making six times fewer sandbox exec calls.
MAX_CHUNK_BYTES = 1536 * 1024
SNAPSHOT_MAX_BYTES = 2 * 1024 * 1024
EXEC_TIMEOUT_SEC = 30
MAX_CONSECUTIVE_FAILURES = 5
MAX_TRIAL_EVENTS = 5000
PAYLOAD_CLIP_CHARS = 2048
_SKIP_TICK = object()
_runtime_redactions: dict[str, dict[str, str]] = {}


def configure_runtime_redactions(trial_id: str, replacements: dict[str, str]) -> None:
    """Stage trial-private exact replacements until its tailer is created."""
    if replacements:
        _runtime_redactions[trial_id] = dict(replacements)


def clear_runtime_redactions(trial_id: str) -> None:
    _runtime_redactions.pop(trial_id, None)


def _redact_exact_text(text: str, replacements: dict[str, str]) -> str:
    for value in sorted(replacements, key=len, reverse=True):
        text = text.replace(value, replacements[value])
    return text


def _redact_exact_value(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _redact_exact_text(value, replacements)
    if isinstance(value, dict):
        return {
            _redact_exact_value(key, replacements): _redact_exact_value(
                item, replacements
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_exact_value(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_exact_value(item, replacements) for item in value)
    return value


def _redact_exact_bytes(data: bytes, replacements: dict[str, str]) -> bytes:
    for value in sorted(replacements, key=len, reverse=True):
        data = data.replace(value.encode("utf-8"), replacements[value].encode("utf-8"))
    return data


class TailExecError(RuntimeError):
    pass


class TailExecUnavailableError(TailExecError):
    pass


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


def _decode_base64(encoded: str, *, source: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise TailExecError(f"invalid base64 {source} output: {exc}") from exc


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clipped_payload(key: str, value: Any, **extra: Any) -> dict[str, Any]:
    sanitized = False
    truncated = False
    if isinstance(value, str):
        safe_text = sanitize_transcript_text(value)
        value = safe_text.text
        sanitized = safe_text.changed
    else:
        safe_value = sanitize_transcript_value(value)
        sanitized = safe_value.changed
        value = json.dumps(safe_value.value, ensure_ascii=False, allow_nan=False)
    truncated = len(value) > PAYLOAD_CLIP_CHARS
    payload: dict[str, Any] = {key: value[:PAYLOAD_CLIP_CHARS]}
    for extra_key, extra_value in extra.items():
        safe_key = sanitize_transcript_text(str(extra_key))
        if isinstance(extra_value, str):
            safe_extra = sanitize_transcript_text(extra_value)
            payload[safe_key.text] = safe_extra.text[:PAYLOAD_CLIP_CHARS]
            sanitized = sanitized or safe_key.changed or safe_extra.changed
            truncated = truncated or len(safe_extra.text) > PAYLOAD_CLIP_CHARS
        else:
            safe_value = sanitize_transcript_value(extra_value)
            payload[safe_key.text] = safe_value.value
            sanitized = sanitized or safe_key.changed or safe_value.changed
    if truncated:
        payload["truncated"] = True
    if sanitized:
        payload["sanitized"] = True
    return payload


def _clip_payload_strings(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        return value[:PAYLOAD_CLIP_CHARS], len(value) > PAYLOAD_CLIP_CHARS
    if isinstance(value, dict):
        clipped: dict[str, Any] = {}
        truncated = False
        for key, item in value.items():
            clipped_item, item_truncated = _clip_payload_strings(item)
            clipped[key] = clipped_item
            truncated = truncated or item_truncated
        return clipped, truncated
    if isinstance(value, list):
        clipped_list: list[Any] = []
        truncated = False
        for item in value:
            clipped_item, item_truncated = _clip_payload_strings(item)
            clipped_list.append(clipped_item)
            truncated = truncated or item_truncated
        return clipped_list, truncated
    return value, False


def _event(kind: str, key: str, value: Any, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "payload": _clipped_payload(key, value, **extra)}


def _with_turn_id(
    events: list[dict[str, Any]], message_id: str
) -> list[dict[str, Any]]:
    """Tag Claude assistant deltas with a stable, opaque turn boundary."""
    turn_id = hashlib.sha256(message_id.encode("utf-8", errors="replace")).hexdigest()
    for event in events:
        payload = event.get("payload")
        if isinstance(payload, dict):
            payload["turn_id"] = turn_id
    return events


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", "")) for block in content if isinstance(block, dict)
        )
    return "" if content is None else str(content)


def _render_assistant_blocks(message: dict) -> list[dict[str, Any]]:
    content = message.get("content")
    rendered: list[dict[str, Any]] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            rendered.append(_event("message", "text", block["text"]))
        elif block.get("type") == "tool_use":
            rendered.append(
                _event(
                    "tool_use",
                    "input",
                    block.get("input") or {},
                    name=str(block.get("name") or ""),
                )
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
    rendered_blocks_by_id: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
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
            if not msg_id:
                return _render_assistant_blocks(message)
            return self._render_assistant_delta(str(msg_id), message)
        if event.get("type") == "user":
            return _render_tool_results(event.get("message"))
        if event.get("type") == "result":
            value = event.get("result") or event.get("subtype") or ""
            return [_event("summary", "text", value)]
        return []

    def _render_assistant_delta(
        self, msg_id: str, message: dict[str, Any]
    ) -> list[dict[str, Any]]:
        content = message.get("content")
        if not isinstance(content, list):
            return []
        previous = self.rendered_blocks_by_id.get(msg_id, [])
        current: list[dict[str, Any]] = []
        rendered: list[dict[str, Any]] = []
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            state = self._block_state(block)
            if state is None:
                continue
            supported_index = len(current)
            prior = (
                previous[supported_index] if supported_index < len(previous) else None
            )
            rendered.extend(
                self._render_block_delta(block, state, prior, supported_index)
            )
            current.append(state)
        self.rendered_blocks_by_id[msg_id] = current
        return _with_turn_id(rendered, msg_id)

    def _block_state(self, block: dict[str, Any]) -> dict[str, Any] | None:
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not text:
                return None
            return {"type": "text", "text": str(text)}
        if block_type == "tool_use":
            return {
                "type": "tool_use",
                "fingerprint": json.dumps(
                    block, sort_keys=True, separators=(",", ":"), default=str
                ),
            }
        return None

    def _render_block_delta(
        self,
        block: dict[str, Any],
        state: dict[str, Any],
        prior: dict[str, Any] | None,
        block_index: int,
    ) -> list[dict[str, Any]]:
        if state["type"] == "text":
            text = state["text"]
            text_mode = "replace"
            prior_text = (
                prior.get("text")
                if prior is not None and prior.get("type") == "text"
                else None
            )
            if prior_text == text:
                return []
            if isinstance(prior_text, str) and text.startswith(prior_text):
                text = text[len(prior_text) :]
                text_mode = "append"
            return (
                [
                    _event(
                        "message",
                        "text",
                        text,
                        block_index=block_index,
                        text_mode=text_mode,
                    )
                ]
                if text
                else []
            )
        if (
            prior is not None
            and prior.get("type") == "tool_use"
            and prior.get("fingerprint") == state["fingerprint"]
        ):
            return []
        return _render_assistant_blocks({"content": [block]})

    def totals(self) -> UsageTotals:
        t = UsageTotals(model=self.model)
        for usage in self.usage_by_id.values():
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            cache_write = int(usage.get("cache_creation_input_tokens") or 0)
            t.input_tokens += (
                int(usage.get("input_tokens") or 0) + cache_read + cache_write
            )
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
        return [_event("message", "text", text)]
    if item_type == "command_execution":
        result = _clipped_payload("content", item.get("aggregated_output") or "")
        exit_code = item.get("exit_code")
        if isinstance(exit_code, int) and exit_code != 0:
            result["is_error"] = True
        return [
            _event(
                "tool_use",
                "input",
                {"command": str(item.get("command") or "")},
                name="shell",
            ),
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
                error.get("message")
                if isinstance(error, dict)
                else event.get("message")
            )
            if not message:
                return []
            return [_event("summary", "text", message)]
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


def _cursor_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()


def _render_cursor_tool_call(event: dict[str, Any]) -> list[dict[str, Any]]:
    if event.get("subtype") != "completed":
        return []
    tool_call = event.get("tool_call")
    if not isinstance(tool_call, dict):
        return []
    rendered: list[dict[str, Any]] = []
    for name, call in tool_call.items():
        if not isinstance(call, dict):
            continue
        args = call.get("args")
        rendered.append(
            _event(
                "tool_use",
                "input",
                args if isinstance(args, dict) else {},
                name=str(name),
            )
        )
        result = call.get("result")
        rendered.append(
            {
                "kind": "tool_result",
                "payload": _clipped_payload(
                    "content", "" if result is None else result
                ),
            }
        )
    return rendered


@dataclass
class CursorUsageFold:
    """Reads cursor-agent's streamed messages and token usage."""

    model: str | None = None
    _totals: UsageTotals = field(default_factory=UsageTotals)
    _seen_usage: bool = False

    @property
    def has_usage(self) -> bool:
        return self._seen_usage

    def on_truncate(self) -> None:
        self._totals = UsageTotals(model=self.model)
        self._seen_usage = False

    def feed_line(self, line: bytes) -> list[dict[str, Any]]:
        event = _parse_json_line(line)
        if event is None:
            return []
        event_type = event.get("type")
        if event_type == "assistant":
            text: Any = _cursor_text(event.get("message"))
        elif event_type == "thinking":
            text = event.get("text") if event.get("subtype") == "completed" else None
        elif event_type == "tool_call":
            return _render_cursor_tool_call(event)
        elif event_type == "result":
            return self._feed_result(event)
        else:
            return []
        if not text:
            return []
        return [_event("message", "text", text)]

    def _feed_result(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        usage = event.get("usage")
        if isinstance(usage, dict):
            self._totals.input_tokens += _as_int(usage.get("inputTokens"))
            self._totals.cache_tokens += _as_int(usage.get("cacheReadTokens"))
            self._totals.cache_write_tokens += _as_int(usage.get("cacheWriteTokens"))
            self._totals.output_tokens += _as_int(usage.get("outputTokens"))
            self._seen_usage = True
        result = event.get("result")
        if not result:
            return []
        return [_event("summary", "text", result)]

    def totals(self) -> UsageTotals:
        return UsageTotals(
            input_tokens=self._totals.input_tokens
            + self._totals.cache_tokens
            + self._totals.cache_write_tokens,
            cache_tokens=self._totals.cache_tokens,
            cache_write_tokens=self._totals.cache_write_tokens,
            output_tokens=self._totals.output_tokens,
            model=self.model,
        )


def _mini_message_usage(message: dict[str, Any]) -> tuple[int, int, int]:
    """Reads a mini-swe message's input, output, and cached token counts."""
    extra = message.get("extra")
    response = extra.get("response") if isinstance(extra, dict) else None
    usage = response.get("usage") if isinstance(response, dict) else None
    if not usage and message.get("object") == "response":
        usage = message.get("usage")
    if not isinstance(usage, dict):
        return 0, 0, 0
    prompt = _as_int(usage.get("prompt_tokens") or usage.get("input_tokens"))
    completion = _as_int(usage.get("completion_tokens") or usage.get("output_tokens"))
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    cached = _as_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    return prompt, completion, cached


def _tool_call_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {"command": raw}
        return parsed if isinstance(parsed, dict) else {"command": raw}
    return {"command": str(raw)}


def _render_mini_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        func = tc.get("function")
        func = func if isinstance(func, dict) else {}
        name = str(func.get("name") or "bash")
        args = _tool_call_arguments(func.get("arguments", {}))
        rendered.append(_event("tool_use", "input", args, name=name))
    return rendered


@dataclass
class MiniSweUsageFold:
    """Reads mini-swe-agent's whole trajectory file that is rewritten each step."""

    model: str | None = None
    emitted: int = 0
    _message_fingerprints: list[str] = field(default_factory=list)
    _totals: UsageTotals = field(default_factory=UsageTotals)
    _has_usage: bool = False

    @property
    def has_usage(self) -> bool:
        return self._has_usage

    def on_truncate(self) -> None:
        return None

    def feed_line(self, line: bytes) -> list[dict[str, Any]]:
        document = _parse_json_line(line)
        if document is None:
            return []
        raw_messages = document.get("messages")
        if isinstance(raw_messages, list):
            messages = raw_messages
            if len(messages) < self.emitted:
                fingerprints = self._fingerprints(messages)
                self.emitted = self._common_prefix_len(fingerprints)
            else:
                fingerprints = self._fingerprints(messages)
        else:
            messages = []
            fingerprints = None
        model = (
            ((document.get("info") or {}).get("config") or {}).get("model") or {}
        ).get("model_name")
        if isinstance(model, str) and model:
            self.model = model
        self._recompute_usage(messages)
        task_index = next(
            (
                i
                for i, m in enumerate(messages)
                if isinstance(m, dict) and m.get("role") == "user"
            ),
            None,
        )
        rendered: list[dict[str, Any]] = []
        for i in range(self.emitted, len(messages)):
            message = messages[i]
            if isinstance(message, dict):
                rendered.extend(self._render_message(message, is_task=i == task_index))
        self.emitted = max(self.emitted, len(messages))
        if fingerprints is not None:
            self._message_fingerprints = fingerprints
        return rendered

    def _fingerprints(self, messages: list[Any]) -> list[str]:
        return [
            json.dumps(message, sort_keys=True, separators=(",", ":"), default=str)
            for message in messages
        ]

    def _common_prefix_len(self, fingerprints: list[str]) -> int:
        common = 0
        for old, new in zip(self._message_fingerprints, fingerprints):
            if old != new:
                break
            common += 1
        return common

    def _recompute_usage(self, messages: list[Any]) -> None:
        input_tokens = output_tokens = cache_tokens = 0
        for message in messages:
            if not isinstance(message, dict):
                continue
            prompt, completion, cached = _mini_message_usage(message)
            input_tokens += prompt
            output_tokens += completion
            cache_tokens += cached
        prior = self._totals.input_tokens + self._totals.output_tokens
        if input_tokens + output_tokens < prior:
            return
        self._totals = UsageTotals(
            input_tokens=input_tokens,
            cache_tokens=cache_tokens,
            output_tokens=output_tokens,
            model=self.model,
        )
        if input_tokens or output_tokens:
            self._has_usage = True

    def _render_message(
        self, message: dict[str, Any], *, is_task: bool
    ) -> list[dict[str, Any]]:
        role = message.get("role")
        content = _tool_result_text(message.get("content"))
        if role == "assistant":
            rendered: list[dict[str, Any]] = []
            if content:
                rendered.append(_event("message", "text", content))
            rendered.extend(_render_mini_tool_calls(message))
            return rendered
        if role == "user" and is_task:
            return []
        if role in ("tool", "user"):
            return [_event("tool_result", "content", content)]
        return []

    def totals(self) -> UsageTotals:
        return self._totals


@dataclass(frozen=True)
class Adapter:
    agent_fragment: str
    log_path: str
    fold_type: type
    snapshot: bool = False

    def matches(self, agent: str) -> bool:
        return self.agent_fragment in agent

    def make_fold(self, model: str | None) -> Fold:
        return cast(Fold, self.fold_type(model=model))


ADAPTERS: tuple[Adapter, ...] = (
    Adapter("claude-code", "/logs/agent/claude-code.txt", ClaudeUsageFold),
    Adapter("codex", "/logs/agent/codex.txt", CodexUsageFold),
    Adapter("cursor-cli", "/logs/agent/cursor-cli.txt", CursorUsageFold),
    Adapter(
        "mini-swe-agent",
        "/logs/agent/mini-swe-agent.trajectory.json",
        MiniSweUsageFold,
        snapshot=True,
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
        snapshot: bool = False,
        runtime_redactions: dict[str, str] | None = None,
    ):
        self.trial_id = trial_id
        self.environment = environment
        self.attempt = attempt
        self.log_path = log_path
        self.fold = fold
        self.snapshot = snapshot
        self.runtime_redactions = dict(runtime_redactions or {})
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
                    self._buffer_events(
                        self.fold.feed_line(
                            _redact_exact_bytes(self.carry, self.runtime_redactions)
                        )
                    )
            with contextlib.suppress(Exception):
                await asyncio.shield(self._persist_tick())

    async def _run_loop(self) -> None:
        failures = 0
        while True:
            stopping = self._stop.is_set()
            try:
                await self._tick()
                failures = 0
            except asyncio.CancelledError:
                raise
            except TailExecUnavailableError as exc:
                console.print(
                    f"[dim]Trial {self.trial_id} live tail disabled: {exc}[/dim]"
                )
                return
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
        if self.snapshot:
            snapshot_raw = await self._read_snapshot()
            if snapshot_raw:
                self._buffer_events(
                    self.fold.feed_line(
                        _redact_exact_bytes(snapshot_raw, self.runtime_redactions)
                    )
                )
        else:
            tail_raw = await self._read_tail()
            if isinstance(tail_raw, bytes) and tail_raw:
                self._feed_tail_chunk(tail_raw)
        await self._persist_tick()

    async def _read_tail(self) -> bytes | object | None:
        command = (
            f"set -o pipefail; wc -c < '{self.log_path}' 2>/dev/null || echo -1; "
            f"tail -c +{self.offset + 1} '{self.log_path}'"
            f" 2>/dev/null | head -c {MAX_CHUNK_BYTES} | base64 | tr -d '\\n'"
        )
        stdout = await self._exec(command, source="tail", tolerate_missing=True)
        if stdout is None:
            return _SKIP_TICK
        size_line, _, encoded = stdout.partition("\n")
        try:
            size = int(size_line.strip())
        except ValueError:
            size, encoded = None, stdout
        if size is not None and 0 <= size < self.offset:
            self.fold.on_truncate()
            self.offset = 0
            self.carry = b""
            return _SKIP_TICK
        encoded = encoded.strip()
        return _decode_base64(encoded, source="tail") if encoded else None

    async def _read_snapshot(self) -> bytes | None:
        command = (
            f"tail -c +1 '{self.log_path}' 2>/dev/null"
            f" | head -c {SNAPSHOT_MAX_BYTES} | base64 | tr -d '\\n'"
        )
        encoded = await self._exec(command, source="snapshot")
        return _decode_base64(encoded, source="snapshot") if encoded else None

    async def _exec(
        self, command: str, *, source: str, tolerate_missing: bool = False
    ) -> str | None:
        result = await self.environment.exec(
            command=command, timeout_sec=EXEC_TIMEOUT_SEC
        )
        return_code = getattr(result, "return_code", 0)
        if return_code not in (0, None):
            if return_code == 127:
                raise TailExecUnavailableError(f"{source} exec failed rc=127")
            if tolerate_missing and return_code != 127 and self.offset == 0:
                return None
            raise TailExecError(f"{source} exec failed rc={return_code}")
        return (result.stdout or "").strip()

    def _feed_tail_chunk(self, raw: bytes) -> None:
        lines, self.carry = split_lines(self.carry + raw)
        self.offset += len(raw)
        for line in lines:
            self._buffer_events(
                self.fold.feed_line(_redact_exact_bytes(line, self.runtime_redactions))
            )

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

    async def _persist_tick(self) -> None:
        checkpoint = self._checkpoint_values()
        if (not self.pending_events or self.replaced) and checkpoint is None:
            return
        flushed_count = 0
        checkpoint_ack: tuple[tuple, float | None] | None = None
        async with get_session() as session:
            flushed_count = await self._flush_events(session)
            checkpoint_ack = await self._checkpoint(session, checkpoint)
        if flushed_count:
            del self.pending_events[:flushed_count]
        if checkpoint_ack is not None:
            self._last_written, self._last_cost = checkpoint_ack

    async def _flush_events(self, session) -> int:
        if not self.pending_events or self.replaced:
            return 0
        events = [self._sanitize_event(event) for event in self.pending_events]
        rows = [
            {"trial_id": self.trial_id, "attempt": self.attempt, **event}
            for event in events
        ]
        stmt = pg_insert(TrialEventModel).values(rows).on_conflict_do_nothing()
        await session.execute(stmt)
        return len(rows)

    def _sanitize_event(self, event: dict[str, Any]) -> dict[str, Any]:
        original_payload = event.get("payload")
        payload = _redact_exact_value(original_payload, self.runtime_redactions)
        # _redact_exact_value scrubs known runtime secrets. Its effect must be counted
        # here too: if it redacted something but the general sanitizer reports no further
        # change, returning the original event would persist the raw secret.
        exact_redacted = payload != original_payload
        safe_payload = sanitize_transcript_value(
            payload if isinstance(payload, dict) else {}
        )
        clipped_payload, truncated = _clip_payload_strings(safe_payload.value)
        if not exact_redacted and not safe_payload.changed and not truncated:
            return event
        normalized = dict(event)
        normalized["payload"] = dict(clipped_payload)
        if exact_redacted or safe_payload.changed:
            normalized["payload"]["sanitized"] = True
        if truncated:
            normalized["payload"]["truncated"] = True
        return normalized

    def _checkpoint_values(self) -> tuple[dict[str, Any], tuple, float | None] | None:
        if not self.fold.has_usage:
            return None
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
            return None
        return values, state, cost

    async def _checkpoint(
        self,
        session,
        checkpoint: tuple[dict[str, Any], tuple, float | None] | None,
    ) -> tuple[tuple, float | None] | None:
        if checkpoint is None:
            return None
        values, state, cost = checkpoint
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
            return None
        return state, cost


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
        snapshot=adapter.snapshot,
        runtime_redactions=_runtime_redactions.get(trial_id),
    )
    old = _tailers.pop(trial_id, None)
    if old:
        old_tailer, old_task = old
        old_tailer.replaced = True
        old_tailer.request_stop()
        old_task.cancel()
        tailer._last_cost = old_tailer._last_cost
        if old_tailer.attempt == attempt:
            tailer.seq = old_tailer.seq
            tailer.capped = old_tailer.capped
            tailer.pending_events = list(old_tailer.pending_events)
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
