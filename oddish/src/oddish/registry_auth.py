from __future__ import annotations

import base64
import hashlib
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DOCKER_HUB_AUTH_KEY = "https://index.docker.io/v1/"
DEFAULT_REGISTRY = "docker.io"
_DOCKER_HUB_ALIASES = {
    "",
    "docker.io",
    "index.docker.io",
    "registry-1.docker.io",
    "https://index.docker.io/v1/",
    "https://registry-1.docker.io",
    "registry.hub.docker.com",
}

current_registry_credentials: ContextVar[list["RegistryCredential"] | None] = (
    ContextVar("current_registry_credentials", default=None)
)


@dataclass(frozen=True)
class RegistryCredential:
    username: str
    token: str
    registry: str = DEFAULT_REGISTRY

    def auth_key(self) -> str:
        host = (self.registry or "").strip().lower()
        if host in _DOCKER_HUB_ALIASES:
            return DOCKER_HUB_AUTH_KEY
        for scheme in ("https://", "http://"):
            if host.startswith(scheme):
                host = host.removeprefix(scheme)
        return host.rstrip("/")

    def to_dict(self) -> dict[str, str]:
        return {
            "registry": self.registry,
            "username": self.username,
            "token": self.token,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "RegistryCredential":
        username = str(raw.get("username") or "").strip()
        token = str(raw.get("token") or raw.get("password") or "")
        registry = str(raw.get("registry") or DEFAULT_REGISTRY).strip()
        if not username or not token:
            raise ValueError("registry credential requires both 'username' and 'token'")
        return cls(username=username, token=token, registry=registry or DEFAULT_REGISTRY)


def normalize_credentials(raw: object) -> list[RegistryCredential]:
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    creds: list[RegistryCredential] = []
    for item in items:
        if isinstance(item, RegistryCredential):
            creds.append(item)
        elif isinstance(item, dict):
            creds.append(RegistryCredential.from_dict(item))
        else:
            raise ValueError(f"unsupported registry credential entry: {type(item)!r}")
    return creds


def build_docker_config_json(creds: list[RegistryCredential]) -> str:
    auths = {
        cred.auth_key(): {
            "auth": base64.b64encode(f"{cred.username}:{cred.token}".encode()).decode()
        }
        for cred in creds
    }
    return json.dumps({"auths": auths})


def _fernet_key(material: str) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest())


def _fernet():
    from cryptography.fernet import Fernet

    from oddish.config import settings

    explicit = getattr(settings, "registry_auth_key", None)
    if explicit:
        try:
            return Fernet(explicit.encode())
        except Exception:
            return Fernet(_fernet_key(explicit))
    return Fernet(_fernet_key(settings.database_url))


def encrypt_credentials(creds: list[RegistryCredential]) -> str | None:
    if not creds:
        return None
    return _fernet().encrypt(json.dumps([c.to_dict() for c in creds]).encode()).decode()


def decrypt_credentials(blob: str | None) -> list[RegistryCredential]:
    if not blob:
        return []
    try:
        return normalize_credentials(json.loads(_fernet().decrypt(blob.encode())))
    except Exception as exc:
        logger.error(
            "Could not decrypt per-run registry credentials (%s); running unauthenticated.",
            type(exc).__name__,
        )
        return []


def _parse_login(value: str) -> RegistryCredential:
    fields: dict[str, str] = {}
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"--registry-login expects key=value pairs, got {part!r}")
        key, _, val = part.partition("=")
        fields[key.strip().lower()] = val.strip()
    return RegistryCredential.from_dict(fields)


def parse_registry_login(values: list[str] | None, env: dict[str, str]) -> list[dict]:
    creds: list[RegistryCredential] = []

    if env.get("ODDISH_DOCKERHUB_USERNAME") and env.get("ODDISH_DOCKERHUB_TOKEN"):
        creds.append(
            RegistryCredential(
                username=env["ODDISH_DOCKERHUB_USERNAME"],
                token=env["ODDISH_DOCKERHUB_TOKEN"],
            )
        )

    creds.extend(_parse_login(value) for value in values or [])

    by_key: dict[str, RegistryCredential] = {}
    for cred in creds:
        by_key[cred.auth_key()] = cred
    return [cred.to_dict() for cred in by_key.values()]
