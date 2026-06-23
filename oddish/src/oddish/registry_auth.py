"""Per-run container-registry credentials for oddish trials.

Why this exists
---------------
A Harbor task whose ``docker-compose.yaml`` pulls public images (``postgres``,
``redis``, the clone services, ...) does so from inside an *unauthenticated*
Docker-in-Docker daemon. Docker Hub rate-limits anonymous pulls per source IP,
so trials fail at environment setup with::

    docker compose up failed: ... toomanyrequests: You have reached your
    unauthenticated pull rate limit.

The fix is to let whoever triggers the run supply *their own* registry login so
the inner daemon pulls authenticated. The credential is per-user and short
lived: it is **never** a shared oddish/Modal secret, **never** stored in the
durable ``trials.harbor_config``, and is scrubbed after the run.

Lifecycle (one credential, many trigger paths)
----------------------------------------------
Every way to trigger an oddish run (the ``oddish run`` CLI, the experiments-repo
and harbor-forge CI workflows, and direct ``POST /tasks/sweep`` API calls) funnels
through the same submission boundary, so a single field carries the credential
everywhere:

1.  **Supply** -- the client attaches ``registry_auth`` to the sweep submission
    (CLI ``--registry-login`` / ``ODDISH_DOCKERHUB_*`` env, or the API field).
2.  **Transit** -- because the queue decouples submit from execution, the
    credential has to cross Postgres. It rides the per-trial ``worker_jobs.payload``
    (NOT ``trials.harbor_config``), Fernet-encrypted with an oddish-managed
    data-protection key. The plaintext token never touches the database.
3.  **Use** -- the worker decrypts it, publishes it on :data:`current_registry_credentials`
    for the duration of the trial, and the Harbor DinD shim
    (``oddish.workers.harbor_patches``) writes ``/root/.docker/config.json`` inside
    the sandbox *before* ``docker compose build``/``up`` so all pulls authenticate.
4.  **Scrub** -- on teardown the shim removes the config file (logs the token off),
    the worker resets the context var, and best-effort drops the ciphertext from
    the ``worker_jobs`` row.

This module owns the data model, the (de)serialization + envelope encryption, the
``~/.docker/config.json`` rendering, and the request-scoped context var.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Canonical auth key Docker uses for Docker Hub regardless of how the user spells
# the registry. The daemon/CLI look up Hub credentials under this exact string.
DOCKER_HUB_AUTH_KEY = "https://index.docker.io/v1/"
_DOCKER_HUB_ALIASES = {
    "",
    "docker.io",
    "index.docker.io",
    "registry-1.docker.io",
    "https://index.docker.io/v1/",
    "https://registry-1.docker.io",
    "registry.hub.docker.com",
}

DEFAULT_REGISTRY = "docker.io"

# Request-scoped registry credentials, published by the worker (see
# ``TrialJobHandler.run``) and read by the Harbor DinD login shim. A context var
# (not a module global) so concurrent trials in one worker never see each other's
# credentials -- each asyncio task gets its own copy.
current_registry_credentials: ContextVar[list["RegistryCredential"] | None] = (
    ContextVar("current_registry_credentials", default=None)
)


@dataclass(frozen=True)
class RegistryCredential:
    """A single ``docker login`` worth of state."""

    username: str
    token: str
    registry: str = DEFAULT_REGISTRY

    def auth_key(self) -> str:
        """Map ``registry`` to the key Docker stores the auth blob under."""
        host = (self.registry or "").strip().lower()
        if host in _DOCKER_HUB_ALIASES:
            return DOCKER_HUB_AUTH_KEY
        # Private registries are keyed by host[:port] without a scheme.
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
    """Coerce assorted input shapes into a list of :class:`RegistryCredential`.

    Accepts a single mapping or a list of mappings. Invalid/empty entries raise
    ``ValueError`` so the CLI/API surface a clear error instead of silently
    dropping a credential the user expected to take effect.
    """
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
    """Render a ``~/.docker/config.json`` body authenticating *creds*.

    The daemon/CLI reads ``auths.<key>.auth`` (base64 ``user:token``) for every
    pull, so writing this single file authenticates both ``compose build`` and
    ``compose up`` without ever invoking ``docker login`` (keeping the raw token
    out of process argv).
    """
    auths: dict[str, dict[str, str]] = {}
    for cred in creds:
        blob = base64.b64encode(f"{cred.username}:{cred.token}".encode()).decode()
        auths[cred.auth_key()] = {"auth": blob}
    return json.dumps({"auths": auths})


# =============================================================================
# Envelope encryption (token never persists in plaintext)
# =============================================================================


def _derive_fernet_key(material: str) -> bytes:
    """Derive a valid Fernet key (urlsafe-b64 of 32 bytes) from *material*."""
    return base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest())


def _resolve_fernet():
    """Return a ``Fernet`` instance shared by the API (encrypt) and worker (decrypt).

    Key precedence:
      1. ``ODDISH_REGISTRY_AUTH_KEY`` if set -- used directly when it is already a
         valid Fernet key, otherwise treated as a passphrase and stretched.
      2. Otherwise derived from ``ODDISH_DATABASE_URL`` (shared by both the API and
         worker containers), so encryption works with zero extra provisioning.
    """
    from cryptography.fernet import Fernet

    from oddish.config import settings

    explicit = getattr(settings, "registry_auth_key", None)
    if explicit:
        try:
            return Fernet(
                explicit if isinstance(explicit, bytes) else explicit.encode()
            )
        except Exception:
            return Fernet(_derive_fernet_key(explicit))
    logger.debug(
        "ODDISH_REGISTRY_AUTH_KEY unset; deriving registry-auth data key from the "
        "database URL. Set ODDISH_REGISTRY_AUTH_KEY for an independent rotation."
    )
    return Fernet(_derive_fernet_key(settings.database_url))


def encrypt_credentials(creds: list[RegistryCredential]) -> str | None:
    """Encrypt *creds* to an opaque token suitable for ``worker_jobs.payload``."""
    if not creds:
        return None
    plaintext = json.dumps([c.to_dict() for c in creds]).encode()
    return _resolve_fernet().encrypt(plaintext).decode()


def decrypt_credentials(blob: str | None) -> list[RegistryCredential]:
    """Inverse of :func:`encrypt_credentials`. Returns ``[]`` on any failure.

    A missing ``blob`` (no credential was supplied) returns ``[]`` quietly. A
    *present but undecryptable* blob is a misconfiguration -- almost always
    ``ODDISH_REGISTRY_AUTH_KEY`` differing between the API and worker, or a key
    rotated after submit -- so it is logged loudly: the trial proceeds
    unauthenticated and would otherwise fail later with an opaque registry
    rate-limit error that gives no hint of the real cause.
    """
    if not blob:
        return []
    try:
        plaintext = _resolve_fernet().decrypt(blob.encode())
        return normalize_credentials(json.loads(plaintext.decode()))
    except Exception as exc:  # never break the trial, but make the cause loud
        logger.error(
            "Could not decrypt per-run registry credentials (%s): the trial will "
            "run UNAUTHENTICATED and may hit registry pull rate limits. This "
            "usually means ODDISH_REGISTRY_AUTH_KEY differs between the API and "
            "worker, or was rotated after the run was submitted -- ensure both "
            "share the same key.",
            type(exc).__name__,
        )
        return []


# =============================================================================
# CLI parsing helper (shared so it can be unit-tested without typer)
# =============================================================================


def parse_registry_login(values: list[str] | None, env: dict[str, str]) -> list[dict]:
    """Build the ``registry_auth`` payload list from CLI flags + environment.

    Sources, later overriding earlier on the same registry key:
      * ``ODDISH_DOCKERHUB_USERNAME`` + ``ODDISH_DOCKERHUB_TOKEN`` -> a docker.io login.
      * ``--registry-login "registry=...,username=...,token=..."`` (repeatable).

    The ``registry=`` part is optional and defaults to docker.io.
    """
    creds: list[RegistryCredential] = []

    hub_user = env.get("ODDISH_DOCKERHUB_USERNAME")
    hub_token = env.get("ODDISH_DOCKERHUB_TOKEN")
    if hub_user and hub_token:
        creds.append(
            RegistryCredential(
                username=hub_user, token=hub_token, registry=DEFAULT_REGISTRY
            )
        )

    for value in values or []:
        fields: dict[str, str] = {}
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise ValueError(
                    f"--registry-login expects key=value pairs, got {part!r}"
                )
            key, _, val = part.partition("=")
            fields[key.strip().lower()] = val.strip()
        creds.append(RegistryCredential.from_dict(fields))

    # De-dup by auth key, last write wins, preserving order.
    by_key: dict[str, RegistryCredential] = {}
    for cred in creds:
        by_key[cred.auth_key()] = cred
    return [c.to_dict() for c in by_key.values()]
