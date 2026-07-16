"""Claude digest generation for ingested documents.

Split so the deterministic parts (prompt build, response parse) are unit-
tested without a network call, and only ``generate_digest`` touches the API
(through the ``oddish.core.llm`` funnel).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

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
    *, title: str, text: str, model: str | None = None
) -> DigestResult:
    """Run the Claude digest call through the LLM funnel. Impure (network)."""
    from oddish.config import settings
    from oddish.core.llm import complete

    result = await complete(
        handler="document_digest",
        prompt=build_digest_prompt(title=title, text=text),
        model=model or settings.digest_model,
        max_tokens=2048,
    )
    return parse_digest_response(result.text)
