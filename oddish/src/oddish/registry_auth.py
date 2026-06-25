from __future__ import annotations

import base64
import hashlib
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import urlsplit

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

_KEY_DOMAIN = b"oddish.registry_auth.v1\x00"

current_registry_credentials: ContextVar[list["RegistryCredential"] | None] = (
    ContextVar("current_registry_credentials", default=None)
)

_warned_about_derived_key = False


class RegistryAuthDecryptError(ValueError):
    """Raised when a present registry-auth blob cannot be decrypted or parsed."""


def normalize_registry_host(registry: str | None) -> str:
    raw = (registry or DEFAULT_REGISTRY).strip()
    if not raw:
        return DEFAULT_REGISTRY
    if raw.lower() in _DOCKER_HUB_ALIASES:
        return DEFAULT_REGISTRY
    if any(ord(ch) < 32 or ch.isspace() for ch in raw):
        raise ValueError("registry must be a host name without whitespace")

    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    host = (parsed.hostname or "").lower()
    if parsed.username or parsed.password:
        raise ValueError("registry must not include username or password")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("registry must be a host name, not a URL path")
    if not host:
        raise ValueError("registry must be a host name")
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return DEFAULT_REGISTRY if host in _DOCKER_HUB_ALIASES else host


@dataclass(frozen=True)
class RegistryCredential:
    username: str
    token: str = field(repr=False)
    registry: str = DEFAULT_REGISTRY

    def auth_key(self) -> str:
        host = normalize_registry_host(self.registry)
        if host in _DOCKER_HUB_ALIASES:
            return DOCKER_HUB_AUTH_KEY
        return host

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
        registry = normalize_registry_host(str(raw.get("registry") or DEFAULT_REGISTRY))
        if not username or not token:
            raise ValueError("registry credential requires both 'username' and 'token'")
        return cls(username=username, token=token, registry=registry)


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
    return json.dumps(
        {
            "auths": {
                cred.auth_key(): {
                    "auth": base64.b64encode(
                        f"{cred.username}:{cred.token}".encode()
                    ).decode()
                }
                for cred in creds
            }
        }
    )


def _derive_fernet_key(material: str) -> bytes:
    return base64.urlsafe_b64encode(
        hashlib.sha256(_KEY_DOMAIN + material.encode()).digest()
    )


def _resolve_fernet():
    from cryptography.fernet import Fernet

    from oddish.config import settings

    explicit = getattr(settings, "registry_auth_key", None)
    if explicit:
        try:
            return Fernet(explicit)
        except Exception:
            return Fernet(_derive_fernet_key(explicit))

    global _warned_about_derived_key
    if not _warned_about_derived_key:
        _warned_about_derived_key = True
        logger.warning(
            "ODDISH_REGISTRY_AUTH_KEY unset; deriving registry-auth key from database URL"
        )
    return Fernet(_derive_fernet_key(settings.database_url))


def encrypt_credentials(creds: list[RegistryCredential]) -> str | None:
    if not creds:
        return None
    plaintext = json.dumps([c.to_dict() for c in creds]).encode()
    return _resolve_fernet().encrypt(plaintext).decode()


def decrypt_credentials(blob: str | None) -> list[RegistryCredential]:
    if not blob:
        return []
    try:
        plaintext = _resolve_fernet().decrypt(blob.encode())
    except Exception as exc:
        logger.error(
            "Could not decrypt per-run registry credentials (%s)",
            type(exc).__name__,
        )
        raise RegistryAuthDecryptError(
            "Could not decrypt per-run registry credentials"
        ) from exc
    try:
        return normalize_credentials(json.loads(plaintext.decode()))
    except Exception as exc:
        logger.error(
            "Could not parse decrypted registry credentials (%s)",
            type(exc).__name__,
        )
        raise RegistryAuthDecryptError(
            "Could not parse decrypted registry credentials"
        ) from exc


_LOGIN_KEYS = ("registry", "username", "token", "password")


def _split_login_pairs(value: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    rest = value.strip()
    while rest:
        key, sep, after = rest.partition("=")
        key = key.strip().lower()
        if not sep or key not in _LOGIN_KEYS:
            raise ValueError(
                "--registry-login expects registry=/username=/token= pairs, "
                f"got {rest!r}"
            )
        end = min(
            (
                boundary
                for known in _LOGIN_KEYS
                if (boundary := after.find(f",{known}=")) != -1
            ),
            default=len(after),
        )
        fields[key] = after[:end].strip()
        rest = after[end:].lstrip(", ")
    return fields


def parse_registry_login(values: list[str] | None, env: dict[str, str]) -> list[dict]:
    hub_user = env.get("ODDISH_DOCKERHUB_USERNAME")
    hub_token = env.get("ODDISH_DOCKERHUB_TOKEN")
    creds = (
        [RegistryCredential(hub_user, hub_token, DEFAULT_REGISTRY)]
        if hub_user and hub_token
        else []
    )
    creds.extend(
        RegistryCredential.from_dict(_split_login_pairs(v)) for v in values or []
    )
    return [c.to_dict() for c in {cred.auth_key(): cred for cred in creds}.values()]
