"""Oddish Antigravity CLI (agy) wrapper for restricted-network trials."""

from __future__ import annotations

from typing import Any

from harbor.agents.installed.antigravity_cli import AntigravityCli

from oddish.workers.harbor.model_hosts import (
    ANTIGRAVITY_INSTALL_HOSTS,
    ANTIGRAVITY_RUNTIME_HOSTS,
    outbound_hosts_for_model,
)


class OddishAntigravityCli(AntigravityCli):
    """agy wrapper for closed-internet trials.

    agy self-installs (curl | bash from antigravity.google) during agent
    SETUP, under the ENVIRONMENT baseline network policy — so the runner's
    antigravity arm merges ``ANTIGRAVITY_INSTALL_HOSTS`` plus the model
    transport host into the environment baseline (see
    ``_antigravity_environment_hosts``), exactly like the opencode arm.

    Unlike ``OddishGeminiCli`` there is no ``disable_web_tools`` switch: agy's
    settings.json has no tool-exclusion layer, so provider-side web tools cannot
    ride around the network boundary: a live closed-network probe showed
    agy's read_url_content and shell curl both fail closed under the egress
    allowlist (agy 1.1.19 reported NO WEB ACCESS). Behaviour is otherwise identical to the stock
    harbor ``AntigravityCli``.
    """

    @classmethod
    def required_outbound_domains(
        cls,
        model_name: str | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> list[str]:
        domains: set[str] = set(ANTIGRAVITY_INSTALL_HOSTS) | set(
            ANTIGRAVITY_RUNTIME_HOSTS
        )
        for host in outbound_hosts_for_model(
            model_name, agent_kwargs=kwargs, infer_bare_provider=True
        ):
            domains.add(host)
        return sorted(domains)
