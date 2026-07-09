"""Upload-time GKE image builder (worker/gke_image_build.py).

Orchestration-level: the harbor-owned pieces (content hash, AR check, Cloud
Build) are monkeypatched at the module seams; runtime parity comes from the
builder importing harbor's own functions on the gke variant image.
"""

import io
import tarfile

import pytest

import worker.gke_image_build as gib

pytestmark = pytest.mark.asyncio


def _archive(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


TASK_TOML = 'version = "1.0"\n[task]\nname = "oddish/pt2jax-nano-adder"\n'


@pytest.fixture
def gke_settings(monkeypatch):
    monkeypatch.setattr(gib.settings, "gke_project_id", "proj")
    monkeypatch.setattr(gib.settings, "gke_region", "us-east5")
    monkeypatch.setattr(gib.settings, "gke_registry_location", "us-east5")
    monkeypatch.setattr(gib.settings, "gke_registry_name", "oddish-envs")


def test_task_short_name_strips_org():
    assert gib._task_short_name({"task": {"name": "oddish/pt2jax-nano-adder"}}) == (
        "pt2jax-nano-adder"
    )
    assert gib._task_short_name({"task": {"name": "bare-name"}}) == "bare-name"
    assert gib._task_short_name({}) == ""


async def test_builds_missing_image_with_derived_url(monkeypatch, gke_settings):
    archive = _archive(
        {"task.toml": TASK_TOML, "environment/Dockerfile": "FROM python:3.11\n"}
    )

    async def fake_fetch(task_id, version):
        return archive

    monkeypatch.setattr(gib, "_fetch_task_archive", fake_fetch)
    monkeypatch.setattr(gib, "_content_hash", lambda env_dir, docker_image: "abc123")
    monkeypatch.setattr(gib, "_image_exists", lambda **kw: False)
    built = []
    monkeypatch.setattr(gib, "_run_cloud_build", lambda **kw: built.append(kw))

    outcome = await gib.ensure_task_image("pt2jax-nano-adder-c470", 1)

    expected = "us-east5-docker.pkg.dev/proj/oddish-envs/pt2jax-nano-adder:abc123"
    assert outcome == f"built {expected}"
    assert built[0]["image_url"] == expected
    assert built[0]["context_dir"].endswith("/environment")


async def test_existing_image_skips_build(monkeypatch, gke_settings):
    archive = _archive(
        {"task.toml": TASK_TOML, "environment/Dockerfile": "FROM python:3.11\n"}
    )

    async def fake_fetch(task_id, version):
        return archive

    monkeypatch.setattr(gib, "_fetch_task_archive", fake_fetch)
    monkeypatch.setattr(gib, "_content_hash", lambda env_dir, docker_image: "abc123")
    monkeypatch.setattr(gib, "_image_exists", lambda **kw: True)

    def must_not_build(**kw):
        raise AssertionError("must not build when the image exists")

    monkeypatch.setattr(gib, "_run_cloud_build", must_not_build)
    assert await gib.ensure_task_image("t", 1) == "exists"


async def test_task_docker_image_needs_no_build(monkeypatch, gke_settings):
    archive = _archive(
        {
            "task.toml": TASK_TOML
            + '[environment]\ndocker_image = "ghcr.io/x/y:tag"\n',
            "environment/Dockerfile": "FROM python:3.11\n",
        }
    )

    async def fake_fetch(task_id, version):
        return archive

    monkeypatch.setattr(gib, "_fetch_task_archive", fake_fetch)
    assert await gib.ensure_task_image("t", 1) == "task-docker-image"


async def test_unconfigured_gke_is_noop(monkeypatch):
    monkeypatch.setattr(gib.settings, "gke_project_id", None)
    assert await gib.ensure_task_image("t", 1) == "no-gke-config"


def test_short_name_matches_harbor_task_config():
    harbor_models = pytest.importorskip("harbor.models.task.config")
    cfg = harbor_models.TaskConfig.model_construct(name="oddish/pt2jax-nano-adder")
    if not hasattr(type(cfg), "short_name"):
        pytest.skip("this harbor pin lacks TaskConfig.short_name (fork-only)")
    assert gib._task_short_name({"task": {"name": cfg.name}}) == cfg.short_name


async def test_missing_region_is_noop(monkeypatch, gke_settings):
    monkeypatch.setattr(gib.settings, "gke_region", None)
    assert await gib.ensure_task_image("t", 1) == "no-gke-config"
