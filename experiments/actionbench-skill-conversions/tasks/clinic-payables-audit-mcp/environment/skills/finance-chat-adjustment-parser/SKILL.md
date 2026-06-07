---
name: finance-chat-adjustment-parser
description: Use this skill when finance reconciliation tasks include correction tokens posted in a chat channel served over a Slack MCP (in message text, attachments, blocks, or thread replies).
---

# Finance Chat Adjustment Parser

## Gathering every message from the Slack MCP

The adjustment tokens are not in the workspace; they are only reachable through
the Slack MCP. Be exhaustive:

1. `slack_list_channels` to find the finance-ops channel and its `channel_id`.
2. Page through `slack_read_channel(channel_id, cursor=next_cursor)` until
   `has_more` is false; scan the inline `attachments` and `blocks` on every page.
3. For each message with `reply_count > 0`, call
   `slack_read_thread(channel_id, thread_ts)` and scan replies.
4. `slack_search_messages` is capped and not exhaustive — do not rely on it for a
   complete tally.

## Token Format

Finance correction tokens appear inline:

```text
FINCORR{type:remap,payment:PAY-1234,invoice:INV-1234,priority:3}
FINCORR{type:hold,invoice:INV-1234,reason:review,priority:7}
FINCORR{type:release,invoice:INV-1234,reason:cleared,priority:5}
```

## Workflow

1. From the gathered text (message `text`, attachment `fallback`/`text`, block
   `text`, thread replies), extract token bodies with a case-insensitive regex
   like `FINCORR\{([^}]+)\}`.
2. Parse comma-separated `key:value` pairs and normalize keys to lowercase.
3. For remaps, group candidates by payment id.
4. For hold/release decisions, group candidates by invoice id (they compete).
5. Select winners by highest priority, then later timestamp on ties.
6. Record losers as superseded and invalid references as skipped.

## Common Mistakes

- Reading only the first page of top-level message text.
- Treating hold and release as separate groups instead of competing decisions for
  the same invoice.
- Applying a remap whose payment or invoice does not exist.
- Forgetting to sort the final winning adjustment timeline by timestamp.
