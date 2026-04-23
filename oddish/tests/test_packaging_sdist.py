from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest


@pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv is required to build the sdist"
)
def test_sdist_includes_cli_sources(tmp_path: Path) -> None:
    package_root = Path(__file__).resolve().parents[1]

    subprocess.run(
        ["uv", "build", "--sdist", "--out-dir", str(tmp_path)],
        cwd=package_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    sdist_path = next(tmp_path.glob("oddish-*.tar.gz"))
    with tarfile.open(sdist_path, "r:gz") as archive:
        names = set(archive.getnames())

    prefix = sdist_path.name.removesuffix(".tar.gz")
    assert f"{prefix}/src/oddish/__init__.py" in names
    assert f"{prefix}/src/oddish/cli/__init__.py" in names
    assert f"{prefix}/src/oddish/cli/run.py" in names
    assert f"{prefix}/src/oddish/analyze/classify_prompt.txt" in names
