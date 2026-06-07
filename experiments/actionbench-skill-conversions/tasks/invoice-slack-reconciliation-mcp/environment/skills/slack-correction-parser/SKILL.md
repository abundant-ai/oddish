---
name: slack-correction-parser
description: Extract structured correction tokens from a live Slack channel (served over an MCP) and apply them to a baseline business dataset. Use when corrections may appear in message text, attachments, blocks, or threaded replies and conflicts are resolved by priority and timestamp.
---

# Slack Correction Parser

Use this workflow when operational corrections live in a Slack channel that is
exposed to you through a Slack MCP server (not as a file on disk).

## Gathering every message from the Slack MCP

The corrections are not in the workspace; they are only reachable through the
Slack MCP tools. Be exhaustive — a single page read or a single search is not
enough:

1. `slack_list_channels` — find the corrections channel and its `channel_id`.
2. `slack_read_channel(channel_id)` — read the first page. The response includes
   `next_cursor` and `has_more`. Keep calling
   `slack_read_channel(channel_id, cursor=next_cursor)` until `has_more` is
   false. Messages are reverse-chronological.
3. Each page renders `attachments` and `blocks` inline — scan those too, not just
   `text`.
4. For every message with `reply_count > 0`, call
   `slack_read_thread(channel_id, thread_ts)` and scan the replies.
5. `slack_search_messages` is a convenience but its results are **capped and not
   exhaustive** — never rely on it alone for a complete tally; use full channel
   pagination plus thread reads.

## Token Extraction

Search the gathered text (message `text`, attachment `fallback`/`pretext`/`text`,
block text, thread replies) for inline tokens. A robust extraction pattern is:

```python
TOKEN_RE = re.compile(r"BILLING_FIX\{([^}]+)\}", re.IGNORECASE)
```

Parse comma-separated `key:value` pairs inside the braces; trim whitespace around
keys and values. Associate each token with the timestamp of its enclosing message
or reply.

## Conflict Resolution

Normalize each parsed correction to `{slack_ts, target_id, field, to, priority}`,
group by target plus field, and pick the winner with:

```python
winner = max(candidates, key=lambda x: (x["priority"], x["slack_ts"]))
```

Slack `ts` strings sort chronologically when zero-padded.

## Audit Pattern

For each group: apply only the winning correction, record losing corrections in a
`superseded` list, record unknown targets in a skipped list, and capture the
previous value before applying the winner.

## Pitfalls

- Corrections hide in thread replies and attachment/block text, not just the
  first page of message text.
- Field-specific type conversion matters: numeric fields become numbers, status
  fields become canonical strings, text fields stay text.
- Some fields are operations rather than replacements (for example additive
  payment events); handle these with field-specific logic and still audit the
  before/after value.
