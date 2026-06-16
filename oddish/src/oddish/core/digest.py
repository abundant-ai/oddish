"""Claude/Bedrock digest generation for ingested documents.

Split so the deterministic parts (prompt build, response parse) are unit-
tested without a network call, and only ``generate_digest`` touches the API.
Reuses the Bedrock routing from ``probe_analysis``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

DEFAULT_DIGEST_MODEL = "claude-sonnet-4-6"

_DIGEST_INSTRUCTIONS = """\
You are preparing a reference document for retrieval by an AI coding agent.
Produce a compact JSON object with exactly these keys:
- "summary": 1-3 sentences a reader scans to decide if the doc is relevant.
- "digest_text": a cleaned, condensed, well-structured rewrite that preserves
  the document's useful facts, steps, and links — optimized for an agent to act
  on without reading the raw source. Use markdown.
- "tags": 3-8 short lowercase keyword tags for filtering.
Return ONLY the JSON object, no prose, no code fence."""


@dataclass
class DigestResult:
    summary: str
    digest_text: str
    tags: list[str] = field(default_factory=list)


def build_digest_prompt(*, title: str, text: str) -> str:
    """Assemble the digest prompt. Pure."""
    return (
        f"{_DIGEST_INSTRUCTIONS}\n\n"
        f"DOCUMENT TITLE: {title}\n\n"
        f"DOCUMENT CONTENT:\n{text}"
    )


def parse_digest_response(raw: str) -> DigestResult:
    """Parse the model's JSON reply, tolerating code fences and bad tags. Pure."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.lstrip().startswith("json"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0].strip()
    data = json.loads(raw)
    tags = data.get("tags")
    if not isinstance(tags, list):
        tags = []
    tags = [str(t) for t in tags][:8]
    return DigestResult(
        summary=str(data.get("summary", "")).strip(),
        digest_text=str(data.get("digest_text", "")).strip(),
        tags=tags,
    )


async def generate_digest(
    *, title: str, text: str, model: str = DEFAULT_DIGEST_MODEL
) -> DigestResult:
    """Run the Claude digest call on the direct Anthropic API. Impure (network).

    Mirrors ``worker.probe_analysis._make_client``: oddish has no SigV4-capable
    Bedrock credential in the API/worker (only an S3-scoped key plus a bearer
    token the pinned SDK can't consume), so internal Claude calls go through the
    direct Anthropic API (``ANTHROPIC_API_KEY``). Routing the digest through
    ``AsyncAnthropicBedrock`` instead 500s every document upload. Normalize any
    Bedrock inference-profile id back to its plain API id so the model reaching
    the client is one the direct API accepts.
    """
    from anthropic import AsyncAnthropic

    from oddish.config import to_anthropic_api_model_id

    resolved = to_anthropic_api_model_id(model) or model
    client = AsyncAnthropic()

    msg = await client.messages.create(
        model=resolved,
        max_tokens=2048,
        messages=[
            {"role": "user", "content": build_digest_prompt(title=title, text=text)}
        ],
    )
    raw = "".join(getattr(b, "text", "") for b in msg.content)
    return parse_digest_response(raw)
