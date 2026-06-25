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
    """The saved registry login cannot be read."""


def normalize_registry_host(registry: str | None) -> str:
    raw = (registry or DEFAULT_REGISTRY).strip()
    if not raw:
        return DEFAULT_REGISTRY
    if raw.lower() in _DOCKER_HUB_ALIASES:
        return DEFAULT_REGISTRY
    if any(ord(ch) < 32 or ch.isspace() for ch in raw):
        raise ValueError("registry must be a host name without whitespace")

    try:
        parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    except ValueError:
        raise ValueError("registry must be a host name") from None
    host = (parsed.hostname or "").lower()
    if parsed.username or parsed.password:
        raise ValueError("registry must not include username or password")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("registry must be a host name, not a URL path")
    if not host:
        raise ValueError("registry must be a host name")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("registry port must be a valid numeric port") from None
    if ":" in host:
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"
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
        raw_token = raw.get("token") or raw.get("password") or ""
        if hasattr(raw_token, "get_secret_value"):
            raw_token = raw_token.get_secret_value()
        token = str(raw_token)
        registry = normalize_registry_host(str(raw.get("registry") or DEFAULT_REGISTRY))
        if not username or not token.strip():
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


_LOGIN_KEYS = {"registry", "username", "token", "password"}


def _split_login_items(value: str) -> list[str]:
    items: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    text = value.strip()
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in {",", "\\", "'", '"'}:
                buf.append(nxt)
                i += 2
                continue
            buf.append(ch)
        elif quote:
            if ch == quote:
                quote = None
                j = i + 1
                while j < len(text) and text[j].isspace():
                    j += 1
                if j < len(text) and text[j] != ",":
                    raise ValueError("--registry-login quotes must wrap the whole value")
            else:
                buf.append(ch)
        elif ch in {"'", '"'}:
            current = "".join(buf)
            _key, sep, value_prefix = current.partition("=")
            if sep and not value_prefix.strip():
                quote = ch
            else:
                raise ValueError("--registry-login quotes must wrap the whole value")
        elif ch == ",":
            items.append("".join(buf).strip())
            buf.clear()
        else:
            buf.append(ch)
        i += 1
    if quote:
        raise ValueError("--registry-login has an unterminated quoted value")
    items.append("".join(buf).strip())
    return items


def _parse_login_pairs(value: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in _split_login_items(value):
        key, sep, raw = item.partition("=")
        key = key.strip().lower()
        if not sep or key not in _LOGIN_KEYS:
            raise ValueError(
                "--registry-login expects comma-separated registry=, username=, "
                "token=, or password= pairs"
            )
        if key in fields:
            raise ValueError(f"--registry-login repeats {key!r}")
        fields[key] = raw.strip()
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
        RegistryCredential.from_dict(_parse_login_pairs(v)) for v in values or []
    )
    return [c.to_dict() for c in {cred.auth_key(): cred for cred in creds}.values()]
