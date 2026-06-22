from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.dispatch.backends.docker import DockerPoolDispatcher
from oddish.dispatch.ports import WorkerHandle


class FakeDockerCLI:
    """A tiny in-memory stand-in for the ``docker`` CLI.

    Models just enough of ``run -d`` / ``inspect`` / ``rm`` for the dispatcher
    contract, so the suite is hermetic (no real Docker daemon).
    """

    def __init__(self) -> None:
        self.containers: dict[str, bool] = {}  # id -> running
        self.commands: list[list[str]] = []
        self._counter = 0

    async def __call__(self, args: list[str]) -> str:
        self.commands.append(args)
        verb = args[0]
        if verb == "run":
            self._counter += 1
            cid = f"ctr{self._counter:012d}"
            self.containers[cid] = True
            return cid + "\n"
        if verb == "inspect":
            cid = args[-1]
            fmt = "{{.State.Running}}" if "-f" in args else ""
            if cid not in self.containers:
                raise RuntimeError(f"No such object: {cid}")
            if fmt:
                return ("true" if self.containers[cid] else "false") + "\n"
            return "[{}]\n"
        if verb == "rm":
            cid = args[-1]
            existed = self.containers.pop(cid, None) is not None
            if not existed:
                raise RuntimeError(f"No such container: {cid}")
            return cid + "\n"
        raise AssertionError(f"unexpected docker verb: {verb}")


def _dispatcher() -> tuple[DockerPoolDispatcher, FakeDockerCLI]:
    cli = FakeDockerCLI()
    dispatcher = DockerPoolDispatcher(
        image="oddish-worker:test",
        run_command=cli,
        env={"ODDISH_DATABASE_URL": "postgres://x"},
    )
    return dispatcher, cli


def test_spawn_runs_one_detached_container_per_queue_key() -> None:
    dispatcher, cli = _dispatcher()

    async def _go():
        return list(await dispatcher.spawn(spawn_plan=["gpt-4o", "claude"]))

    handles = asyncio.run(_go())
    assert [h.queue_key for h in handles] == ["gpt-4o", "claude"]
    assert all(h.provider == "docker" and h.id and not h.provisional for h in handles)
    run_cmds = [c for c in cli.commands if c[0] == "run"]
    assert len(run_cmds) == 2
    # detached, labelled for tag-based GC, and the queue_key is passed through.
    for cmd in run_cmds:
        assert "-d" in cmd
        assert "oddish-worker:test" in cmd
        assert any("oddish_managed=1" in part for part in cmd)
    assert any("gpt-4o" in part for part in run_cmds[0])
    assert any("claude" in part for part in run_cmds[1])


def test_check_active_yields_only_running_containers() -> None:
    dispatcher, cli = _dispatcher()

    async def _go():
        handles = list(await dispatcher.spawn(spawn_plan=["a", "b"]))
        # Stop the first container out of band.
        await cli(["rm", handles[0].id])
        return [h async for h in dispatcher.check_active(handles)]

    live = asyncio.run(_go())
    assert len(live) == 1
    assert live[0].queue_key == "b"


def test_cancel_removes_containers_and_counts() -> None:
    dispatcher, cli = _dispatcher()

    async def _go():
        handles = list(await dispatcher.spawn(spawn_plan=["a", "b"]))
        first = await dispatcher.cancel(handles)
        second = await dispatcher.cancel(handles)
        return first, second

    first, second = asyncio.run(_go())
    assert first == 2
    assert second == 0


def test_recover_returns_handle_for_existing_container() -> None:
    dispatcher, cli = _dispatcher()

    async def _go():
        (handle,) = list(await dispatcher.spawn(spawn_plan=["a"]))
        recovered = await dispatcher.recover(handle.serialize())
        gone = await dispatcher.recover(
            WorkerHandle(provider="docker", queue_key="a", id="missing").serialize()
        )
        return recovered, gone

    recovered, gone = asyncio.run(_go())
    assert recovered is not None and recovered.id.startswith("ctr")
    assert gone is None


def test_docker_dispatcher_import_is_lazy() -> None:
    # The dispatcher shells out to the docker CLI; importing it must not require
    # the docker SDK or the modal SDK.
    src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        f"import sys; sys.path.insert(0, {src!r})\n"
        "import builtins\n"
        "_real = builtins.__import__\n"
        "def _guard(name, *a, **k):\n"
        "    if name in ('modal', 'docker') or name.startswith(('modal.', 'docker.')):\n"
        "        raise AssertionError('forbidden import at module load: ' + name)\n"
        "    return _real(name, *a, **k)\n"
        "builtins.__import__ = _guard\n"
        "import oddish.dispatch.backends.docker\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
