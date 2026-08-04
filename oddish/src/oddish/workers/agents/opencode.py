from __future__ import annotations

from typing import Any

from harbor.agents.installed.opencode import OpenCode

from oddish.workers.harbor.model_hosts import (
    OPENCODE_INSTALL_HOSTS,
    outbound_hosts_for_model,
)


class OddishOpenCode(OpenCode):
    """opencode wrapper for closed-internet trials.

    opencode has no pre-baked worker image: Harbor's ``OpenCode.install``
    bootstraps nvm, a Node runtime, and the ``opencode-ai`` npm package at trial
    start. The egress allowlist actually enforced on a closed-internet Modal
    trial is built by ``_inject_restricted_agent_model_hosts`` (runner.py) as
    the union of ``outbound_hosts_for_model`` (model transport; resolves
    ``openrouter/<model>`` -> ``openrouter.ai``) and ``agent_runtime_hosts``,
    which reads ``_AGENT_RUNTIME_HOSTS`` in model_hosts.py -- where
    ``OPENCODE_INSTALL_HOSTS`` is registered under both the stock agent name and
    this wrapper's class name. Without that registration, agent setup dies at
    DNS before the model is ever dialed:

        curl: (6) Could not resolve host: raw.githubusercontent.com
        Error: NVM failed to load

    ``required_outbound_domains`` below has no consumer in oddish or harbor
    today (verified against harbor 504c2518, and end-to-end on the PR preview:
    a closed-internet opencode trial still died at install with only the hook
    declared). It is kept for interface parity with the other Oddish agent
    wrappers (codex, grok-build, mini-swe-agent) that declare the same hook, so
    the full egress contract lives on the class if a consumer lands. The
    enforced path is the ``_AGENT_RUNTIME_HOSTS`` registration.

    Behaviour is otherwise identical to the stock harbor ``OpenCode`` agent.
    """

    @classmethod
    def required_outbound_domains(
        cls,
        model_name: str | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> list[str]:
        """Full egress contract: install-bootstrap hosts + model transport.

        Currently declarative only -- nothing in oddish or harbor calls this
        hook (see class docstring); the enforced allowlist comes from
        ``_AGENT_RUNTIME_HOSTS`` + ``outbound_hosts_for_model``. Kept accurate
        so a future consumer inherits the correct union.
        """
        domains: set[str] = set(OPENCODE_INSTALL_HOSTS)
        # Forward the per-trial ``kwargs`` so a transport host pinned in
        # ``kwargs["extra_env"]`` (e.g. a custom ``OPENROUTER_BASE_URL``) is
        # included too, mirroring ``AzureCompatibleCodex``.
        for host in outbound_hosts_for_model(
            model_name, agent_kwargs=kwargs, infer_bare_provider=True
        ):
            domains.add(host)
        return sorted(domains)
