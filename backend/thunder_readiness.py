"""Minimal Modal probe for the deployed Thunder worker image and credentials."""

from __future__ import annotations

from importlib.metadata import version

from modal_app import app, image, runtime_secrets, thunder_worker_secrets


def thunder_worker_readiness() -> dict[str, str | bool]:
    """Validate SDK imports and resolved credentials without exposing secrets."""
    import aiohttp
    import asyncssh
    import cryptography
    from thunder_sandbox import ClientConfig

    # Importing each module is itself part of the check. Distribution metadata
    # alone could pass even when an import-time dependency is broken.
    del aiohttp, asyncssh, cryptography

    config = ClientConfig()
    if not config.api_url:
        raise RuntimeError("Thunder API URL did not resolve")
    if not config.api_token:
        raise RuntimeError("Thunder API token did not resolve")

    sdk_version = version("thunder-sandbox")
    if sdk_version != "0.5.0":
        raise RuntimeError(
            f"Expected thunder-sandbox 0.5.0, found {sdk_version}"
        )
    return {
        "thunder_sandbox": sdk_version,
        "aiohttp": version("aiohttp"),
        "asyncssh": version("asyncssh"),
        "cryptography": version("cryptography"),
        "api_url_resolved": True,
        "api_token_resolved": True,
    }


@app.function(
    image=image,
    secrets=[*runtime_secrets, *thunder_worker_secrets],
    timeout=60,
    cpu=1.0,
    memory=512,
)
def check_thunder_worker() -> dict[str, str | bool]:
    """Operator deploy check using the real Thunder worker image and secret."""
    return thunder_worker_readiness()
