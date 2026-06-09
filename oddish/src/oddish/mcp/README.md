# Oddish Doc-Store MCP Server

Exposes the agent doc-store to a networked Claude Code session as three
read-only MCP tools:

| Tool | Returns | Use when |
|------|---------|----------|
| `search_documents(query, tags?)` | lightweight cards (`id`, `title`, `summary`, `tags`), title matches first | finding candidates |
| `get_document(id)` | the agent-optimized **digest** | reading a candidate |
| `inspect_source(id)` | the **full extracted source text** | the digest lost a detail |

The tiers keep agent context cheap: scan cards → read one digest → only
escalate to the full source when needed.

## Configuration

The server connects directly to Postgres and is scoped to a single org for
its whole lifetime.

| Env var | Required | Meaning |
|---------|----------|---------|
| `ODDISH_DATABASE_URL` | yes | Postgres connection (same as the backend) |
| `ODDISH_DOCSTORE_ORG_ID` | no | Org to scope retrieval to. Unset = global/unscoped. |

**Runtime deps:** the server needs `oddish[server]` (SQLAlchemy/asyncpg) plus
`mcp` — both are pulled in by the `[server]` extra. Run it from an environment
that installed that extra (e.g. the `backend` project, which depends on
`oddish[worker,dev]`). The plain `oddish` base venv does **not** include them.

## Register with Claude Code

Add to your MCP config (`.mcp.json` / Claude Code settings):

```json
{
  "mcpServers": {
    "oddish-docstore": {
      "command": "uv",
      "args": ["--project", "/abs/path/to/backend", "run", "oddish-docstore-mcp"],
      "env": {
        "ODDISH_DATABASE_URL": "postgresql+asyncpg://...",
        "ODDISH_DOCSTORE_ORG_ID": "org_xxx"
      }
    }
  }
}
```

The server speaks MCP over stdio. Ingest documents via the backend
`POST /documents` API or the frontend doc-store page; this server is
retrieval-only.
