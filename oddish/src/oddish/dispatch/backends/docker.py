"""Docker dispatcher: one detached worker container per job (design spec §9-D).

The portability proof's fan-out primitive. Each spawned worker is a one-shot
``docker run -d`` of the oddish worker image draining a single ``queue_key``;
the container exits when its job (or batch) finishes — the Modal-container model,
off Modal. Containers are labelled so the reconciler can list-by-tag and GC
leaks (the CRI/Nomad pattern, spec §6.5).

Shells out to the ``docker`` CLI (injected for tests) rather than depending on
the docker SDK, so the package stays import-light and adds no Python dependency.
Container ids are durable handles, so ``recover`` reattaches after a
control-plane restart (Nomad RecoverTask).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Sequence

from oddish.dispatch.ports import Dispatcher, WorkerHandle

logger = logging.getLogger(__name__)

RunCommand = Callable[[list[str]], Awaitable[str]]

# Tag every managed container so the reconciler can list + reclaim leaks.
MANAGED_LABEL = "oddish_managed=1"
QUEUE_KEY_LABEL = "oddish_queue_key"


async def _default_run_command(args: list[str]) -> str:
    """Run ``docker <args>`` and return stdout (raising on non-zero exit)."""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args)} failed ({proc.returncode}): "
            f"{stderr.decode(errors='replace').strip()}"
        )
    return stdout.decode(errors="replace")


class DockerPoolDispatcher:
    name = "docker"

    def __init__(
        self,
        *,
        image: str,
        run_command: RunCommand | None = None,
        env: Mapping[str, str] | None = None,
        network: str | None = None,
        command: Sequence[str] = ("python", "-m", "oddish.workers.queue.worker"),
        extra_run_args: Sequence[str] = (),
    ) -> None:
        self._image = image
        self._run: RunCommand = run_command or _default_run_command
        self._env = dict(env or {})
        self._network = network
        self._command = list(command)
        self._extra_run_args = list(extra_run_args)

    async def spawn(self, *, spawn_plan: Sequence[str]) -> Sequence[WorkerHandle]:
        handles: list[WorkerHandle] = []
        for queue_key in spawn_plan:
            args = [
                "run",
                "-d",
                "--rm",
                "--label",
                MANAGED_LABEL,
                "--label",
                f"{QUEUE_KEY_LABEL}={queue_key}",
            ]
            if self._network:
                args += ["--network", self._network]
            for key, value in self._env.items():
                args += ["-e", f"{key}={value}"]
            # The worker drains its assigned queue_key only.
            args += ["-e", f"ODDISH_WORKER_QUEUE_KEY={queue_key}"]
            args += list(self._extra_run_args)
            args.append(self._image)
            args += self._command
            container_id = (await self._run(args)).strip()
            handles.append(
                WorkerHandle(
                    provider=self.name,
                    queue_key=queue_key,
                    id=container_id,
                    provisional=False,
                )
            )
        return handles

    async def list_managed(self) -> list[WorkerHandle]:
        """List every running container this dispatcher manages (by label).

        The tag-based reclamation source (spec §6.5): cross-reference against
        live ``worker_jobs`` rows to find leaks. (Containers also self-clean via
        ``--rm`` on exit, so this is a backstop.)
        """
        out = await self._run(["ps", "-q", "--filter", f"label={MANAGED_LABEL}"])
        return [
            WorkerHandle(provider=self.name, queue_key="", id=cid.strip())
            for cid in out.splitlines()
            if cid.strip()
        ]

    async def check_active(
        self, handles: Sequence[WorkerHandle]
    ) -> AsyncIterator[WorkerHandle]:
        for handle in handles:
            if not handle.id:
                continue
            try:
                out = await self._run(
                    ["inspect", "-f", "{{.State.Running}}", handle.id]
                )
            except Exception:
                continue  # gone -> not active
            if out.strip() == "true":
                yield handle

    async def cancel(self, handles: Sequence[WorkerHandle]) -> int:
        cancelled = 0
        for handle in handles:
            if not handle.id:
                continue
            try:
                await self._run(["rm", "-f", handle.id])
                cancelled += 1
            except Exception:
                # Already gone / never existed -> nothing cancelled.
                continue
        return cancelled

    async def recover(self, serialized: Mapping[str, Any]) -> WorkerHandle | None:
        handle = WorkerHandle.deserialize(serialized)
        if not handle.id:
            return None
        try:
            await self._run(["inspect", handle.id])
        except Exception:
            return None
        return handle


_: Dispatcher = DockerPoolDispatcher(image="placeholder")  # structural check
