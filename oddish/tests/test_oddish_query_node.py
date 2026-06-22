import shutil, subprocess
from pathlib import Path
import pytest

ASSETS = Path(__file__).resolve().parents[1] / "src" / "oddish" / "assets"

@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_node_cli_suite():
    r = subprocess.run(["node", "--test"], cwd=ASSETS, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
