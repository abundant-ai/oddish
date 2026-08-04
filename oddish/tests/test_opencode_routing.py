from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.workers.agents.opencode import OddishOpenCode  # noqa: E402
from oddish.workers.harbor.model_hosts import agent_runtime_hosts  # noqa: E402


def test_opencode_wrapper_keeps_stock_agent_identity():
    # The wrapper must keep harbor's agent name so ``-a opencode`` resolves to it.
    assert OddishOpenCode.name() == "opencode"


def test_agent_runtime_hosts_registers_opencode_install_hosts():
    """The ENFORCED closed-internet path: ``_inject_restricted_agent_model_hosts``
    unions ``agent_runtime_hosts`` into the trial allowlist, and that reads the
    static ``_AGENT_RUNTIME_HOSTS`` registry -- ``required_outbound_domains`` has
    no consumer. Without this entry a closed-internet opencode trial dies at DNS
    during install (observed end-to-end on the PR preview backend)."""
    hosts = agent_runtime_hosts(agent_name="opencode")

    for host in ("raw.githubusercontent.com", "registry.npmjs.org", "nodejs.org"):
        assert host in hosts


def test_agent_runtime_hosts_resolves_oddish_opencode_wrapper_key():
    """``_apply_opencode_oddish_wrapper`` nulls ``name`` in favor of an import
    path, so the runtime-host lookup must also hit on the wrapper's class name."""
    hosts = agent_runtime_hosts(
        agent_name=None,
        import_path="oddish.workers.agents.opencode:OddishOpenCode",
    )

    assert "raw.githubusercontent.com" in hosts


def test_opencode_required_outbound_domains_includes_openrouter_transport():
    """OpenRouter-served models (e.g. ``openrouter/tencent/hy3``) must be reachable
    on a closed-internet trial, or Harbor's Modal firewall blackholes every model
    request and the agent times out with zero tokens."""
    domains = OddishOpenCode.required_outbound_domains(
        model_name="openrouter/tencent/hy3", kwargs={}
    )

    assert "openrouter.ai" in domains


def test_opencode_required_outbound_domains_includes_runtime_install_hosts():
    """opencode self-installs nvm / Node / opencode-ai at trial start; those hosts
    must be allowlisted or agent setup dies at DNS before the model is reached
    (``curl: (6) Could not resolve host: raw.githubusercontent.com``)."""
    domains = OddishOpenCode.required_outbound_domains(
        model_name="openrouter/tencent/hy3"
    )

    for host in ("raw.githubusercontent.com", "registry.npmjs.org", "nodejs.org"):
        assert host in domains


def test_opencode_required_outbound_domains_resolves_non_openrouter_provider():
    """The model transport is resolved from the model id, so an xAI-served model
    allowlists the xAI host rather than silently granting no model egress."""
    domains = OddishOpenCode.required_outbound_domains(model_name="xai/grok-4.5")

    assert "api.x.ai" in domains


def test_opencode_required_outbound_domains_allowlists_per_trial_base_url():
    """A trial that pins a custom transport host via ``kwargs["extra_env"]`` must
    have that host allowlisted -- the per-trial route ``kwargs`` are forwarded to
    ``outbound_hosts_for_model``, mirroring ``AzureCompatibleCodex``. Without the
    forward, a closed-internet trial overriding its base URL is firewalled."""
    domains = OddishOpenCode.required_outbound_domains(
        model_name="openrouter/tencent/hy3",
        kwargs={
            "extra_env": {
                "OPENROUTER_BASE_URL": "https://gateway.internal.example/api"
            }
        },
    )

    assert "gateway.internal.example" in domains
