from __future__ import annotations

from typing import Any, AsyncIterator

import httpx


class AgentSandboxClient:
    """Outbound client to the agent-sandbox-service. Bearer auth via API key."""

    def __init__(self, *, base_url: str, api_key: str, timeout_s: float = 90.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- Probes ---
    async def submit_probe(
        self,
        *,
        org_id: str,
        user_id: str | None,
        task_id: str,
        agent: str,
        model: str,
        extra_instructions: str = "",
        timeout_minutes: int = 30,
        idempotency_key: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        body = {
            "org_id": org_id, "user_id": user_id, "task_id": task_id,
            "agent": agent, "model": model,
            "extra_instructions": extra_instructions,
            "timeout_minutes": timeout_minutes,
            "metadata": metadata or {},
        }
        r = await self._client.post("/v1/probes", json=body, headers=headers)
        r.raise_for_status()
        return r.json()["probe_run_id"]

    async def get_probe(self, probe_run_id: str) -> dict[str, Any]:
        r = await self._client.get(f"/v1/probes/{probe_run_id}")
        r.raise_for_status()
        return r.json()

    async def list_probes(self, *, org_id: str, **filters) -> dict[str, Any]:
        params = {"org_id": org_id, **{k: v for k, v in filters.items() if v is not None}}
        r = await self._client.get("/v1/probes", params=params)
        r.raise_for_status()
        return r.json()

    async def cancel_probe(self, probe_run_id: str) -> None:
        r = await self._client.post(f"/v1/probes/{probe_run_id}/cancel")
        r.raise_for_status()

    # --- Chat ---
    async def start_chat_session(
        self,
        *,
        org_id: str,
        user_id: str | None,
        scope: dict[str, Any],
    ) -> str:
        body = {"org_id": org_id, "user_id": user_id, "scope": scope}
        r = await self._client.post("/v1/chat-sessions", json=body)
        r.raise_for_status()
        return r.json()["session_id"]

    async def get_chat_session(self, session_id: str) -> dict[str, Any]:
        r = await self._client.get(f"/v1/chat-sessions/{session_id}")
        r.raise_for_status()
        return r.json()

    async def stream_chat_message(
        self, *, session_id: str, content: str
    ) -> AsyncIterator[bytes]:
        """Yields raw SSE bytes the FE can pass through unchanged."""
        async with self._client.stream(
            "POST",
            f"/v1/chat-sessions/{session_id}/messages",
            json={"content": content},
        ) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes():
                yield chunk

    async def close_chat_session(self, session_id: str) -> None:
        r = await self._client.delete(f"/v1/chat-sessions/{session_id}")
        r.raise_for_status()

    async def export_chat_skills(self, session_id: str) -> bytes:
        r = await self._client.get(f"/v1/chat-sessions/{session_id}/skills.tar.gz")
        r.raise_for_status()
        return r.content

    # --- Trials ---
    async def get_trajectory_summary(
        self, *, trial_id: str, s3_prefix: str
    ) -> dict[str, Any]:
        r = await self._client.get(
            f"/v1/trials/{trial_id}/trajectory/summary",
            params={"s3_prefix": s3_prefix},
        )
        r.raise_for_status()
        return r.json()
