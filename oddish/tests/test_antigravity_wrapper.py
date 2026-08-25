from __future__ import annotations

from harbor.agents.installed.antigravity_cli import AntigravityCli

from oddish.workers.agents.antigravity_cli import OddishAntigravityCli
from oddish.workers.harbor.model_hosts import ANTIGRAVITY_INSTALL_HOSTS


def test_antigravity_wrapper_subclasses_stock_agent():
    # The wrapper must keep harbor's agent identity/behaviour, only adding the
    # egress contract on top.
    assert issubclass(OddishAntigravityCli, AntigravityCli)


def test_antigravity_required_outbound_domains_includes_install_and_model_hosts():
    """agy self-installs from antigravity.google during agent SETUP; those hosts
    must be allowlisted alongside the model transport host, or agent setup dies
    at DNS before the model is ever reached, mirroring OddishOpenCode."""
    domains = OddishAntigravityCli.required_outbound_domains(
        model_name="google/gemini-3.7-flash"
    )

    assert domains == sorted(domains)
    for host in ANTIGRAVITY_INSTALL_HOSTS:
        assert host in domains
    assert "generativelanguage.googleapis.com" in domains


def test_antigravity_required_outbound_domains_without_model_still_has_install_hosts():
    # Even with no model resolved yet, the install hosts must be present so
    # agent SETUP can complete.
    domains = OddishAntigravityCli.required_outbound_domains(model_name=None)

    for host in ANTIGRAVITY_INSTALL_HOSTS:
        assert host in domains
