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
    bootstraps nvm, a Node runtime, and the ``opencode-ai`` npm package during
    agent SETUP -- which runs under the ENVIRONMENT baseline network policy,
    before the agent-phase allowlist ever applies. The enforced fix is the
    runner's opencode arm (mirroring the claude-code installer arm in
    ``run_harbor_trial_async``): ``OPENCODE_INSTALL_HOSTS`` plus the model
    transport host are merged into ``env_config.extra_allowed_hosts``, which
    harbor folds into the environment baseline so the allowlist spans install
    *and* run. On a legacy closed task (``[environment] allow_internet=false``
    -> no-network baseline, no dynamic restricted agent phase) that channel is
    also the only one granting the model transport host. Without it, agent
    setup dies at DNS before the model is ever dialed:

        curl: (6) Could not resolve host: raw.githubusercontent.com
        Error: NVM failed to load

    ``required_outbound_domains`` below has no consumer in oddish or harbor
    today (verified against harbor 504c2518, and end-to-end on the PR preview:
    a closed-internet opencode trial still died at install with only the hook
    declared). It is kept for interface parity with the other Oddish agent
    wrappers (codex, grok-build, mini-swe-agent) that declare the same hook, so
    the full egress contract lives on the class if a consumer lands.

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
        hook (see class docstring); the enforced allowlist comes from the
        runner's environment-baseline arm. Kept accurate so a future consumer
        inherits the correct union.
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
