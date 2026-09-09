"""Inspect the installed ``oddish`` package and build a PyPI self-upgrade."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Literal

import httpx

PACKAGE_NAME = "oddish"
PYPI_JSON_URL = "https://pypi.org/pypi/oddish/json"

Source = Literal["pypi", "editable", "other"]
Manager = Literal["uv-pip", "pip"]


class PackageError(Exception):
    """The current install cannot be described or upgraded safely."""


@dataclass(frozen=True)
class InstallInfo:
    version: str
    source: Source
    manager: Manager
    installer: str | None = None
    editable_path: str | None = None

    @property
    def source_label(self) -> str:
        if self.source == "pypi":
            return "PyPI"
        if self.source == "editable":
            return f"editable ({self.editable_path or 'local checkout'})"
        return "non-PyPI"

    @property
    def manager_label(self) -> str:
        return "uv pip" if self.manager == "uv-pip" else "pip"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": PACKAGE_NAME,
            "version": self.version,
            "source": self.source,
            "manager": self.manager,
            "installer": self.installer,
            "editable_path": self.editable_path,
        }


def installed_version() -> str:
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        from oddish import __version__

        return __version__


def inspect_install(
    *,
    which: Callable[[str], str | None] = shutil.which,
    distribution: metadata.Distribution | None = None,
) -> InstallInfo:
    version = installed_version()
    installer: str | None = None
    source: Source = "pypi"
    editable_path: str | None = None
    try:
        dist = distribution or metadata.distribution(PACKAGE_NAME)
        raw_installer = dist.read_text("INSTALLER")
        if raw_installer:
            installer = raw_installer.strip() or None
        source, editable_path = _source_from_direct_url(
            dist.read_text("direct_url.json")
        )
    except metadata.PackageNotFoundError:
        pass
    return InstallInfo(
        version=version,
        source=source,
        manager="uv-pip" if which("uv") else "pip",
        installer=installer,
        editable_path=editable_path,
    )


def upgrade_command(
    info: InstallInfo,
    *,
    executable: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    force: bool = False,
    pin_version: str | None = None,
) -> list[str]:
    if info.source == "editable":
        location = info.editable_path or "this checkout"
        raise PackageError(
            f"This is an editable install from {location}. "
            "Pull the checkout and reinstall from there; "
            "`oddish update` will not overwrite a development tree."
        )
    if info.source != "pypi":
        raise PackageError(
            "`oddish update` currently supports PyPI installs "
            "(`uv pip install oddish`). Reinstall with that command, then retry."
        )

    package = f"{PACKAGE_NAME}=={pin_version}" if pin_version else PACKAGE_NAME
    python = executable or sys.executable
    if info.manager == "uv-pip":
        if which("uv") is None:
            raise PackageError("`uv` is not on PATH. Install it or reinstall oddish with pip.")
        command = ["uv", "pip", "install", "--python", python]
        if force:
            command.extend(["--reinstall-package", PACKAGE_NAME])
        return [*command, "--upgrade", package]
    command = [python, "-m", "pip", "install"]
    if force:
        command.extend(["--force-reinstall", "--no-deps"])
    return [*command, "--upgrade", package]


def fetch_pypi_latest() -> str:
    try:
        payload = httpx.get(PYPI_JSON_URL, timeout=10.0).json()
    except httpx.HTTPError as exc:
        raise PackageError(f"Could not reach PyPI: {exc}") from exc
    version = payload.get("info", {}).get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise PackageError("PyPI did not return a package version.")
    return version.strip()


def is_outdated(current: str, latest: str) -> bool:
    return _version_key(latest) > _version_key(current)


def query_installed_version(executable: str) -> str:
    completed = subprocess.run(
        [executable, "-c", "from importlib.metadata import version; print(version('oddish'))"],
        check=False,
        capture_output=True,
        text=True,
    )
    version = completed.stdout.strip()
    if completed.returncode != 0 or not version:
        raise PackageError(
            completed.stderr.strip() or "Could not read the installed oddish version."
        )
    return version


def _source_from_direct_url(raw_direct: str | None) -> tuple[Source, str | None]:
    if not raw_direct:
        return "pypi", None
    try:
        parsed = json.loads(raw_direct)
    except json.JSONDecodeError:
        return "pypi", None
    if not isinstance(parsed, dict):
        return "pypi", None
    url = parsed.get("url")
    url_text = url if isinstance(url, str) and url else None
    dir_info = parsed.get("dir_info")
    if isinstance(dir_info, dict) and dir_info.get("editable"):
        return "editable", url_text
    if parsed.get("vcs_info") or (url_text and url_text.startswith("file:")):
        return "other", None
    return "pypi", None


def _version_key(value: str) -> tuple[int, ...]:
    numbers: list[int] = []
    for part in value.split("."):
        match = re.match(r"\d+", part)
        if not match:
            break
        numbers.append(int(match.group()))
    return tuple(numbers) or (0,)
