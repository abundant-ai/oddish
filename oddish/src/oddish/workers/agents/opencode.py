from __future__ import annotations

from typing import Any

from harbor.agents.installed.opencode import OpenCode

from oddish.workers.harbor.model_hosts import outbound_hosts_for_model

# opencode has no pre-baked worker image: Harbor's ``OpenCode.install`` bootstraps
# the agent at trial start by downloading nvm, a Node runtime, and the
# ``opencode-ai`` npm package (see
# ``harbor.agents.installed.opencode.OpenCode.install``). On a closed-internet
# trial (``allow_internet=false``) Harbor's Modal egress firewall allowlists only
# the hosts ``required_outbound_domains`` returns, so a stock opencode trial is
# firewalled during agent setup and dies before the model is ever reached:
#
#     curl: (6) Could not resolve host: raw.githubusercontent.com
#     Error: NVM failed to load
#
# These are the hosts that bootstrap chain dials. They are intentionally the
# narrowest set that lets the documented install script complete; see the PR
# description for why baking opencode into the worker image is the preferred
# longer-term fix (it removes the runtime install entirely, so the agent phase
# need only reach the model host).
_OPENCODE_INSTALL_HOSTS: tuple[str, ...] = (
    "raw.githubusercontent.com",  # nvm install.sh
    "github.com",  # nvm git source + opencode-ai release redirect
    "objects.githubusercontent.com",  # GitHub release-asset CDN
    "codeload.github.com",  # GitHub tarball fetch
    "nodejs.org",  # Node runtime downloaded by nvm
    "registry.npmjs.org",  # npm metadata + package tarballs
)


class OddishOpenCode(OpenCode):
    """opencode wrapper that declares its Modal closed-internet egress allowlist.

    Mirrors the ``AzureCompatibleCodex`` fix (CHANGELOG #416): Harbor builds the
    per-trial Modal egress firewall allowlist from ``required_outbound_domains``,
    so an agent that self-installs at runtime *and* talks to an arbitrary model
    provider must declare BOTH its install hosts and its model transport host, or
    every closed-internet trial is firewalled and times out with zero tokens.

    Behaviour is otherwise identical to the stock harbor ``OpenCode`` agent; the
    only override is the egress declaration.
    """

    @classmethod
    def required_outbound_domains(
        cls,
        model_name: str | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> list[str]:
        """Egress domains for Harbor's Modal firewall allowlist.

        The union of opencode's runtime-install bootstrap hosts and the model
        transport host(s) for ``model_name``. ``outbound_hosts_for_model`` is the
        single source of truth for provider host resolution and already handles
        OpenRouter (``openrouter/<model>`` -> ``openrouter.ai``) -- the transport
        for models such as ``openrouter/tencent/hy3`` -- alongside every other
        routed provider.
        """
        domains: set[str] = set(_OPENCODE_INSTALL_HOSTS)
        # Forward the per-trial ``kwargs`` so a transport host pinned in
        # ``kwargs["extra_env"]`` (e.g. a custom ``OPENROUTER_BASE_URL``) is
        # allowlisted too -- the same path ``AzureCompatibleCodex`` relies on.
        # Without it, a closed-internet trial that overrides its base URL is
        # firewalled at the model call after install.
        for host in outbound_hosts_for_model(
            model_name, agent_kwargs=kwargs, infer_bare_provider=True
        ):
            domains.add(host)
        return sorted(domains)
