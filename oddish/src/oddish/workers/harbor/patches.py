"""Patch Harbor at runtime."""

from __future__ import annotations

import base64
import importlib
import inspect
import json
import logging
import os
import re
import shlex
import tempfile
from weakref import WeakSet
from functools import partial
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_PATCHED = False
_EC2_PATCHED_CLASSES: WeakSet[type[Any]] = WeakSet()

_EC2_MANAGED_TAG_KEY = "oddish:managed"
_EC2_AWS_ACCOUNT_ID_TAG_KEY = "oddish:aws-account-id"

_MIRROR_URL = "https://mirror.gcr.io"
_MIRROR_HOST = "mirror.gcr.io"
_DAEMON_JSON_PATH = "/etc/docker/daemon.json"

_DOCKERD_DIAG_CMD = (
    "echo '=== tail -n 200 /var/log/dockerd.log ==='; "
    "tail -n 200 /var/log/dockerd.log 2>&1 || echo '(dockerd.log unavailable)'; "
    "echo '=== dockerd process ==='; "
    "ps -ef 2>/dev/null | grep -i dockerd | grep -v grep || echo '(no dockerd process)'; "
    "echo '=== memory (MB) ==='; free -m 2>/dev/null || echo '(free unavailable)'; "
    "echo '=== disk ==='; df -h /var/lib/docker 2>/dev/null || df -h 2>/dev/null || true"
)

_DOCKER_CONFIG_PATH = "/root/.docker/config.json"
_DOCKER_CONFIG_STAGED_PATH = "/tmp/oddish-docker-config.registry-auth.json"
_REGISTRY_LOGIN_CMD = """
set -eu
staged="/tmp/oddish-docker-config.registry-auth.json"
mkdir -p /root/.docker
cp "$staged" /root/.docker/config.json
chmod 600 /root/.docker/config.json
rm -f "$staged"
""".strip()
_LOGGED_IN_ATTR = "_oddish_registry_logged_in"


def apply_harbor_patches(*, require_ec2: bool = False) -> None:
    """Install Harbor patches once."""
    global _PATCHED
    if not _PATCHED:
        _patch_restricted_network_runtime_fields()
        _patch_daytona_dind()
        _patch_modal_dind()
        _PATCHED = True
    _patch_ec2_lifecycle(require_ec2=require_ec2)


def _patch_restricted_network_runtime_fields() -> None:
    """Teach Harbor to consume Oddish's non-serialized runtime fields.

    The fields are attached only to restricted Daytona Compose AgentConfig
    instances. Public, single-container, Modal, Kubernetes, and local Docker
    configs never carry them, so their behavior remains byte-for-byte the
    upstream path.
    """
    from harbor.agents.factory import AgentFactory
    from harbor.trial import network_policy as network_policy_module

    from .restricted_network import (
        RUNTIME_ALLOWED_HOSTS_ATTR,
        RUNTIME_MODEL_NAME_ATTR,
    )

    original_policy = network_policy_module.resolve_agent_phase_policy
    if not getattr(original_policy, "_oddish_runtime_fields_wrapped", False):

        def resolve_agent_phase_policy(
            task_cfg: Any,
            trial_agent_cfg: Any,
            agent_env_baseline: Any,
            step_cfg: Any = None,
        ) -> Any:
            policy = original_policy(
                task_cfg,
                trial_agent_cfg,
                agent_env_baseline,
                step_cfg,
            )
            runtime_hosts = tuple(
                getattr(trial_agent_cfg, RUNTIME_ALLOWED_HOSTS_ATTR, ()) or ()
            )
            if not runtime_hosts:
                return policy
            return network_policy_module.merge_extra_allowlists(
                policy,
                network_policy_module.normalize_allowed_hosts(list(runtime_hosts)),
            )

        setattr(
            resolve_agent_phase_policy,
            "_oddish_runtime_fields_wrapped",
            True,
        )
        network_policy_module.resolve_agent_phase_policy = resolve_agent_phase_policy

    # Idempotency is primarily the module-level _PATCHED guard in
    # apply_harbor_patches; this second check keeps re-wrapping impossible even
    # if this function is invoked directly. Record the marker on the AgentFactory
    # class and read it from the same place, so the guard cannot be defeated by
    # classmethod/bound-method attribute forwarding.
    if not getattr(AgentFactory, "_oddish_runtime_fields_wrapped", False):
        original_factory = AgentFactory.create_agent_from_config.__func__

        def create_agent_from_config(
            cls: type[Any],
            config: Any,
            logs_dir: Any,
            **kwargs: Any,
        ) -> Any:
            runtime_model = getattr(config, RUNTIME_MODEL_NAME_ATTR, None)
            if not runtime_model:
                return original_factory(cls, config, logs_dir, **kwargs)

            runtime_config = config.model_copy(deep=False)
            runtime_config.model_name = runtime_model
            agent = original_factory(cls, runtime_config, logs_dir, **kwargs)

            # Keep provider/deployment details in the running agent, but report
            # the submitted public model identity in result.json.
            public_model = config.model_name
            if public_model:
                if "/" in public_model:
                    provider, name = public_model.split("/", 1)
                else:
                    provider, name = None, public_model
                agent._parsed_model_provider = provider
                agent._parsed_model_name = name
            return agent

        AgentFactory.create_agent_from_config = classmethod(create_agent_from_config)
        AgentFactory._oddish_runtime_fields_wrapped = True


def _ec2_enabled() -> bool:
    return os.environ.get("ODDISH_EC2_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def _collect_dockerd_diagnostics(strategy: Any) -> str:
    """Read dockerd logs."""
    try:
        result = await strategy._vm_exec(_DOCKERD_DIAG_CMD, timeout_sec=15)
        out = (getattr(result, "stdout", "") or "") + (
            getattr(result, "stderr", "") or ""
        )
        out = out.strip() or "(no diagnostic output)"
        return (
            "dockerd diagnostics (captured by oddish before sandbox teardown):\n" + out
        )
    except Exception as exc:
        return f"dockerd diagnostics unavailable: {exc!r}"


def _redact(text: str, secrets: list[str]) -> str:
    out = text
    for secret in sorted(
        {secret for secret in secrets if secret}, key=len, reverse=True
    ):
        out = out.replace(secret, "***")
    return out


def _result_text(result: Any) -> str:
    return (getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")


def _secret_variants(secret: str) -> list[str]:
    quoted = shlex.quote(secret)
    json_secret = json.dumps(secret)[1:-1]
    json_quoted = json.dumps(quoted)[1:-1]
    return [
        secret,
        quoted,
        repr(secret)[1:-1],
        repr(quoted)[1:-1],
        json_secret,
        json_quoted,
        repr(json_secret)[1:-1],
        repr(json_quoted)[1:-1],
    ]


def _registry_login_config(creds: list[Any]) -> tuple[str, list[str], list[str]]:
    auths: dict[str, dict[str, str]] = {}
    secrets: list[str] = []
    registries: list[str] = []
    for cred in creds:
        registry = cred.auth_key()
        if registry == "https://index.docker.io/v1/":
            auth_key = "https://index.docker.io/v1/"
            registry_label = "docker.io"
        else:
            auth_key = registry
            registry_label = registry
        registries.append(registry_label)
        basic = f"{cred.username}:{cred.token}"
        encoded_basic = base64.b64encode(basic.encode()).decode()
        auths[auth_key] = {"auth": encoded_basic}
        secrets.extend(_secret_variants(cred.token))
        secrets.extend(_secret_variants(basic))
        secrets.extend(_secret_variants(encoded_basic))
    return json.dumps({"auths": auths}), secrets, registries


async def _upload_text(strategy: Any, text: str, target_path: str) -> None:
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(text)
            tmp_name = tmp.name
        uploaded = strategy._stage_file_to_host(tmp_name, target_path)
        if inspect.isawaitable(uploaded):
            await uploaded
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


async def _perform_registry_login(strategy: Any) -> None:
    from oddish.registry_auth import current_registry_credentials

    creds = current_registry_credentials.get()
    if not creds:
        return
    if getattr(strategy, _LOGGED_IN_ATTR, False):
        return

    config_json, secrets, registries = _registry_login_config(creds)
    logged_registries = sorted(set(registries))
    setattr(strategy, _LOGGED_IN_ATTR, True)
    labels = ", ".join(logged_registries)
    message: str | None = None
    try:
        await _upload_text(strategy, config_json, _DOCKER_CONFIG_STAGED_PATH)
        result = await strategy._vm_exec(
            _REGISTRY_LOGIN_CMD,
            timeout_sec=30,
        )
    except Exception as exc:
        message = f"Registry login failed for {labels}: {_redact(repr(exc), secrets)}"
        logger.warning(message)
    if message is not None:
        raise RuntimeError(message)
    if getattr(result, "return_code", 1) != 0:
        message = (
            f"Registry login failed (rc={getattr(result, 'return_code', '?')}) "
            f"for {labels}: {_redact(_result_text(result), secrets)}"
        )
        logger.warning(message)
        raise RuntimeError(message)
    logger.info("Authenticated DinD daemon for registries: %s", labels)


def _wrap_wait_for_docker_daemon(
    orig: Callable[[Any], Awaitable[None]],
    *,
    with_diagnostics: bool = False,
    with_mirror: bool = False,
) -> Callable[[Any], Awaitable[None]]:
    async def _wait_for_docker_daemon(self: Any) -> None:
        try:
            await orig(self)
        except RuntimeError as exc:
            if with_diagnostics:
                diag = await _collect_dockerd_diagnostics(self)
                raise RuntimeError(f"{exc}\n\n{diag}") from exc
            raise
        if with_mirror:
            await _inject_mirror_and_reload(self)
        await _perform_registry_login(self)

    setattr(_wait_for_docker_daemon, "_oddish_wrapped", True)
    return _wait_for_docker_daemon


def _install_method_patch(
    module_path: str,
    class_name: str,
    method_name: str,
    wrap: Callable[[Any], Any],
    *,
    marker: str,
    label: str,
) -> None:
    try:
        module = importlib.import_module(module_path)
    except Exception:
        logger.debug("Harbor %s unavailable; skipping %s patch", module_path, label)
        return

    cls = getattr(module, class_name, None)
    orig = getattr(cls, method_name, None)
    if orig is None:
        logger.warning(
            "Harbor %s.%s not found; %s patch skipped", class_name, method_name, label
        )
        return
    if getattr(orig, marker, False):
        return

    setattr(cls, method_name, wrap(orig))
    logger.info("Installed oddish %s patch on Harbor %s", label, class_name)


_DAYTONA_DOCKERD_MARKER = "dockerd-entrypoint.sh dockerd"
_REGISTRY_MIRROR_FLAG_RE = re.compile(
    r"\s+--registry-mirror(?:=(\"[^\"]*\"|'[^']*'|\S+)|\s+(\"[^\"]*\"|'[^']*'|\S+))"
)


def _mirror_daemon_json(raw: str) -> str:
    try:
        cfg = json.loads(raw or "{}")
    except json.JSONDecodeError:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    mirrors = cfg.get("registry-mirrors")
    if not isinstance(mirrors, list):
        mirrors = []
    cfg["registry-mirrors"] = [_MIRROR_URL, *[m for m in mirrors if m != _MIRROR_URL]]
    return json.dumps(cfg)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _raise_for_bad_result(result: Any) -> None:
    code = getattr(result, "return_code", 0)
    if code not in (None, 0):
        stderr = getattr(result, "stderr", "") or ""
        stdout = getattr(result, "stdout", "") or ""
        raise RuntimeError((stderr or stdout or f"command failed with {code}").strip())


def _strip_registry_mirror_flags(command: str) -> str:
    return _REGISTRY_MIRROR_FLAG_RE.sub("", command)


def _daytona_command_with_mirror(command: str) -> str:
    if _DAYTONA_DOCKERD_MARKER not in command:
        return command
    before, after = command.split(_DAYTONA_DOCKERD_MARKER, 1)
    # Keep any executable prefix (for example ``/usr/local/bin/``) attached to
    # the daemon command in both branches.  Putting the prefix before the
    # conditional would turn an absolute entrypoint into ``/usr/local/bin/if``.
    daemon_command = f"{_DAYTONA_DOCKERD_MARKER}{after}"
    existing = command
    config_command = f"{before}{_strip_registry_mirror_flags(daemon_command)}"
    if _MIRROR_URL in daemon_command:
        mirrored = existing
    else:
        mirrored = (
            f"{before}{_DAYTONA_DOCKERD_MARKER} --registry-mirror={_MIRROR_URL}{after}"
        )
    return (
        f"if grep -q '\"registry-mirrors\"' {_DAEMON_JSON_PATH} "
        f"2>/dev/null; then\n"
        f"{config_command}\n"
        f"else\n{mirrored}\nfi"
    )


def _wrap_daytona_vm_exec(
    orig: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    async def _vm_exec(self: Any, command: str, *args: Any, **kwargs: Any) -> Any:
        command = _daytona_command_with_mirror(command)
        return await orig(self, command, *args, **kwargs)

    setattr(_vm_exec, "_oddish_mirror_wrapped", True)
    return _vm_exec


async def _inject_mirror_and_reload(strategy: Any) -> None:
    try:
        res = await strategy._vm_exec(
            f"cat {_DAEMON_JSON_PATH} 2>/dev/null || echo '{{}}'", timeout_sec=10
        )
        daemon_json_b64 = _b64(_mirror_daemon_json(getattr(res, "stdout", "")))

        result = await strategy._vm_exec(
            "umask 077 && mkdir -p /etc/docker && "
            f"printf %s '{daemon_json_b64}' | base64 -d > {_DAEMON_JSON_PATH}.tmp "
            f"&& mv {_DAEMON_JSON_PATH}.tmp {_DAEMON_JSON_PATH}",
            timeout_sec=10,
        )
        _raise_for_bad_result(result)
        await strategy._vm_exec(
            'kill -HUP "$(cat /var/run/docker.pid 2>/dev/null '
            '|| pidof dockerd 2>/dev/null)" 2>/dev/null || true',
            timeout_sec=10,
        )

        check = await strategy._vm_exec(
            "docker info --format '{{json .RegistryConfig.Mirrors}}' 2>/dev/null",
            timeout_sec=10,
        )
        out = (getattr(check, "stdout", "") or "").strip()
        if _MIRROR_HOST not in out:
            logger.warning(
                "Modal DinD registry mirror not confirmed by docker info: %r", out
            )
        else:
            logger.info("Pointed Modal DinD dockerd at registry mirror %s", _MIRROR_URL)
    except Exception as exc:
        logger.warning("Failed to inject registry mirror into Modal DinD: %r", exc)


def _patch_daytona_dind() -> None:
    module_path = "harbor.environments.daytona.environment"
    class_name = "_DaytonaDinD"
    _install_method_patch(
        module_path,
        class_name,
        "_wait_for_docker_daemon",
        partial(_wrap_wait_for_docker_daemon, with_diagnostics=True, with_mirror=False),
        marker="_oddish_wrapped",
        label="dockerd-diagnostics+registry-login",
    )
    _install_method_patch(
        module_path,
        class_name,
        "_vm_exec",
        _wrap_daytona_vm_exec,
        marker="_oddish_mirror_wrapped",
        label="registry-mirror",
    )


def _patch_modal_dind() -> None:
    module_path = "harbor.environments.modal"
    class_name = "_ModalDinD"
    _install_method_patch(
        module_path,
        class_name,
        "_wait_for_docker_daemon",
        partial(_wrap_wait_for_docker_daemon, with_diagnostics=False, with_mirror=True),
        marker="_oddish_wrapped",
        label="registry-mirror+registry-login",
    )


def _patch_ec2_lifecycle(*, require_ec2: bool = False) -> None:
    try:
        module = importlib.import_module("harbor.environments.ec2")
    except Exception as exc:
        if require_ec2:
            raise RuntimeError(
                "EC2 trial requires Harbor's EC2 environment, but it is unavailable"
            ) from exc
        logger.debug("Harbor EC2 environment unavailable; skipping EC2 lifecycle patch")
        return
    cls = getattr(module, "EC2Environment", None)
    if cls is None:
        if require_ec2:
            raise RuntimeError(
                "EC2 trial requires Harbor to expose EC2Environment"
            )
        logger.warning("Harbor EC2Environment not found; EC2 lifecycle patch skipped")
        return
    if cls in _EC2_PATCHED_CLASSES:
        return
    original_run_instances_kwargs = getattr(cls, "_run_instances_kwargs", None)
    if original_run_instances_kwargs is None:
        if require_ec2:
            raise RuntimeError(
                "EC2 trial requires Harbor EC2Environment to expose "
                "_run_instances_kwargs"
            )
        logger.warning(
            "Harbor EC2Environment._run_instances_kwargs not found; EC2 launch patch skipped"
        )
        return

    def get_sandbox_id(self: Any) -> str | None:
        instance_id = getattr(self, "instance_id", None)
        user_tags = getattr(self, "user_tags", None)
        if not isinstance(user_tags, dict) or user_tags.get(
            _EC2_MANAGED_TAG_KEY
        ) != "true":
            return instance_id
        account_id = user_tags.get(_EC2_AWS_ACCOUNT_ID_TAG_KEY)
        region = getattr(self, "region", None)
        if not all(
            isinstance(value, str) and value
            for value in (account_id, region, instance_id)
        ):
            raise RuntimeError(
                "Oddish-managed EC2 environment is missing account, region, "
                "or instance identity"
            )
        return f"ec2://{account_id}/{region}/{instance_id}"

    def run_instances_kwargs(self: Any) -> dict[str, Any]:
        kwargs = original_run_instances_kwargs(self)
        if kwargs.get("IamInstanceProfile"):
            raise RuntimeError("EC2 instance profiles are forbidden for Oddish sandboxes")
        kwargs["MetadataOptions"] = {"HttpEndpoint": "disabled"}
        return kwargs

    cls.provider_name = "ec2"
    cls.get_sandbox_id = get_sandbox_id
    cls._run_instances_kwargs = run_instances_kwargs
    _EC2_PATCHED_CLASSES.add(cls)
