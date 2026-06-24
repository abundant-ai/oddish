"""Per-run container-registry logins that ride the queue encrypted."""

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

# Domain-separates the derived key so it can't collide with any other sha256 of
# the same database URL.
_KEY_DOMAIN = b"oddish.registry_auth.v1\x00"

# Per-task credentials the worker publishes and the DinD login shim reads. A
# context var (not a global) so concurrent trials never see each other's creds.
current_registry_credentials: ContextVar[list["RegistryCredential"] | None] = (
    ContextVar("current_registry_credentials", default=None)
)

_warned_about_derived_key = False


@dataclass(frozen=True)
class RegistryCredential:
    """One docker login: a username, a token, and which registry."""

    username: str
    token: str
    registry: str = DEFAULT_REGISTRY

    def auth_key(self) -> str:
        """The key docker stores this login under in config.json."""
        host = (self.registry or "").strip().lower()
        if host in _DOCKER_HUB_ALIASES:
            return DOCKER_HUB_AUTH_KEY
        for scheme in ("https://", "http://"):
            if host.startswith(scheme):
                host = host[len(scheme) :]
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
        registry = (
            str(raw.get("registry") or DEFAULT_REGISTRY).strip() or DEFAULT_REGISTRY
        )
        if not username or not token:
            raise ValueError("registry credential requires both 'username' and 'token'")
        return cls(username=username, token=token, registry=registry)


def normalize_credentials(raw: object) -> list[RegistryCredential]:
    """Coerce a mapping (or list of them) into RegistryCredentials."""
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
    """Render the ~/.docker/config.json that authenticates every pull."""
    auths: dict[str, dict[str, str]] = {}
    for cred in creds:
        blob = base64.b64encode(f"{cred.username}:{cred.token}".encode()).decode()
        auths[cred.auth_key()] = {"auth": blob}
    return json.dumps({"auths": auths})


def _derive_fernet_key(material: str) -> bytes:
    """Stretch any string into a valid Fernet key."""
    return base64.urlsafe_b64encode(
        hashlib.sha256(_KEY_DOMAIN + material.encode()).digest()
    )


def _resolve_fernet():
    """The Fernet the API encrypts with and the worker decrypts with."""
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
            "ODDISH_REGISTRY_AUTH_KEY unset; deriving the registry-auth key from "
            "the database URL. Set ODDISH_REGISTRY_AUTH_KEY for an independent, "
            "rotatable key."
        )
    return Fernet(_derive_fernet_key(settings.database_url))


def encrypt_credentials(creds: list[RegistryCredential]) -> str | None:
    """Encrypt creds into an opaque token for worker_jobs.payload."""
    if not creds:
        return None
    plaintext = json.dumps([c.to_dict() for c in creds]).encode()
    return _resolve_fernet().encrypt(plaintext).decode()


def decrypt_credentials(blob: str | None) -> list[RegistryCredential]:
    """Inverse of encrypt_credentials; returns [] (loudly) on any failure."""
    if not blob:
        return []
    try:
        plaintext = _resolve_fernet().decrypt(blob.encode())
    except Exception as exc:
        logger.error(
            "Could not decrypt per-run registry credentials (%s); the trial will "
            "run UNAUTHENTICATED and may hit registry pull rate limits. "
            "ODDISH_REGISTRY_AUTH_KEY likely differs between the API and worker, "
            "or was rotated after the run was submitted -- both must share one key.",
            type(exc).__name__,
        )
        return []
    try:
        return normalize_credentials(json.loads(plaintext.decode()))
    except Exception as exc:
        logger.error(
            "Decrypted the registry credentials but could not parse them (%s); the "
            "trial will run UNAUTHENTICATED.",
            type(exc).__name__,
        )
        return []


_LOGIN_KEYS = ("registry", "username", "token", "password")


def _split_login_pairs(value: str) -> dict[str, str]:
    """Read 'username=U,token=T[,registry=R]', letting the token keep any commas."""
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
        end = len(after)
        for known in _LOGIN_KEYS:
            boundary = after.find(f",{known}=")
            if boundary != -1 and boundary < end:
                end = boundary
        fields[key] = after[:end].strip()
        rest = after[end:].lstrip(", ")
    return fields


def parse_registry_login(values: list[str] | None, env: dict[str, str]) -> list[dict]:
    """Build the registry_auth payload from --registry-login flags + DOCKERHUB env."""
    creds: list[RegistryCredential] = []

    hub_user = env.get("ODDISH_DOCKERHUB_USERNAME")
    hub_token = env.get("ODDISH_DOCKERHUB_TOKEN")
    if hub_user and hub_token:
        creds.append(RegistryCredential(hub_user, hub_token, DEFAULT_REGISTRY))

    for value in values or []:
        creds.append(RegistryCredential.from_dict(_split_login_pairs(value)))

    by_key: dict[str, RegistryCredential] = {}
    for cred in creds:
        by_key[cred.auth_key()] = cred
    return [c.to_dict() for c in by_key.values()]
