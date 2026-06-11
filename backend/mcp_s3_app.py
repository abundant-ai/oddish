"""Modal deployment of the per-experiment S3 MCP server.

Serves the streamable-http MCP ASGI app from ``oddish.mcp.s3_app`` as a Modal
web endpoint, reusing the same image + secrets the API uses so the server has
DB + S3 credentials (``runtime_secrets`` already bundles the AWS-credentials
secret alongside ``oddish-prod``).
"""

from __future__ import annotations

import modal

from modal_app import app, image, runtime_secrets


@app.function(
    image=image,
    secrets=runtime_secrets,
    min_containers=0,
)
@modal.asgi_app()
def s3_mcp_asgi():
    """Streamable-http MCP server scoped per experiment via signed token."""
    from oddish.mcp.s3_app import create_app

    return create_app()
