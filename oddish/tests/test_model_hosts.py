from oddish.workers.harbor.model_hosts import (
    agent_runtime_hosts,
    outbound_hosts_for_model,
)


def test_outbound_hosts_follow_model_not_agent_env_shape():
    # claude-code harness + fireworks model → Fireworks host, not Anthropic.
    assert outbound_hosts_for_model("fireworks/glm-5.2") == ["api.fireworks.ai"]
    assert outbound_hosts_for_model("zai/glm-4.6") == ["api.z.ai"]
    assert outbound_hosts_for_model("moonshot/kimi-k2.5") == ["api.moonshot.ai"]
    assert outbound_hosts_for_model("minimax/MiniMax-M3") == ["api.minimax.io"]
    assert outbound_hosts_for_model("xai/grok-4") == ["api.x.ai"]
    assert outbound_hosts_for_model("anthropic/claude-opus-4-8") == [
        "api.anthropic.com",
        "mcp-proxy.anthropic.com",
    ]
    assert outbound_hosts_for_model("anthropic-hdo/claude-opus-4-8") == [
        "api.anthropic.com",
        "mcp-proxy.anthropic.com",
    ]


def test_outbound_hosts_preserve_legacy_union():
    hosts = outbound_hosts_for_model(
        "openai/model-with-explicit-route",
        agent_env={"OPENAI_BASE_URL": "https://api.fireworks.ai/inference"},
    )
    assert hosts == ["api.fireworks.ai", "api.openai.com", "ab.chatgpt.com"]


def test_outbound_hosts_bedrock_model_ids():
    hosts = outbound_hosts_for_model("global.anthropic.claude-sonnet-4-5-20250929-v1:0")
    assert "bedrock-runtime.us-east-1.amazonaws.com" in hosts
    assert "sts.amazonaws.com" in hosts


def test_outbound_hosts_bare_claude_api_id():
    # claude-code under force-direct-API routing gets the bare Anthropic id
    # (the provider prefix is stripped so the CLI accepts it). Harbor derives
    # the agent allowlist from this id, so it must still resolve to the API.
    assert outbound_hosts_for_model("claude-opus-4-8") == [
        "api.anthropic.com",
        "mcp-proxy.anthropic.com",
    ]
    assert outbound_hosts_for_model("claude-sonnet-5") == [
        "api.anthropic.com",
        "mcp-proxy.anthropic.com",
    ]


def test_bare_claude_id_does_not_override_routed_base_url():
    # A provider-routed base URL still wins over the bare-id fallback.
    assert outbound_hosts_for_model(
        "claude-sonnet-4",
        agent_env={"ANTHROPIC_BASE_URL": "https://api.fireworks.ai/inference"},
    ) == ["api.fireworks.ai"]


def test_outbound_hosts_for_cursor_model():
    cursor_hosts = ["*.cursor.sh"]

    assert outbound_hosts_for_model("cursor/composer") == cursor_hosts


def test_outbound_hosts_read_cursor_custom_endpoint():
    hosts = outbound_hosts_for_model(
        "custom-model",
        agent_env={"CURSOR_API_BASE_URL": "https://cursor-proxy.example/v1"},
    )
    assert hosts == ["cursor-proxy.example"]


def test_outbound_hosts_read_gemini_custom_endpoint():
    hosts = outbound_hosts_for_model(
        "google/gemini-test",
        agent_env={"GOOGLE_GEMINI_BASE_URL": "https://gemini-relay.example/v1"},
    )
    assert "gemini-relay.example" in hosts


def test_bare_model_inference_is_opt_in_and_does_not_widen_single_container():
    # The single-container host union (default call) must NOT add a provider's
    # public default hosts for a bare model id -- only the routed base URL.
    # Bare-provider inference is opt-in, used solely by restricted-Compose host
    # inference; without it a bare id + custom *_BASE_URL would wrongly unlock the
    # public provider hosts.
    assert outbound_hosts_for_model("gpt-4o") == []
    assert outbound_hosts_for_model(
        "gpt-4o", agent_env={"OPENAI_BASE_URL": "https://custom.test/v1"}
    ) == ["custom.test"]
    # Opt-in inference resolves the bare provider's default hosts.
    assert "api.openai.com" in outbound_hosts_for_model(
        "gpt-4o", infer_bare_provider=True
    )


def test_bare_classifier_only_provider_infers_host_opt_in_only():
    # A bare classifier-only-provider id (e.g. xai "grok-4") resolves to its host
    # under opt-in restricted-Compose inference, but the single-container union
    # path leaves it empty (no widening).
    assert outbound_hosts_for_model("grok-4", infer_bare_provider=True) == ["api.x.ai"]
    assert outbound_hosts_for_model("grok-4") == []


def test_tbh_gets_its_own_service_host_which_the_model_id_never_names():
    # Regression: a meta/ model resolves the OpenAI-compatible model API
    # (api.ai.meta.com) while the tbh harness dials api.meta.ai for its model
    # catalog and inference. A trial allowlisted from the model id alone failed
    # with "failed to fetch model catalog: transport error".
    assert outbound_hosts_for_model("meta/striking_tomcat172") == ["api.ai.meta.com"]
    assert agent_runtime_hosts(agent_name="tbh") == ["api.meta.ai"]


def test_agent_runtime_hosts_are_empty_for_provider_talking_agents():
    # Every other agent reaches the endpoint its model id names, so it must not
    # gain a host here.
    assert agent_runtime_hosts(agent_name="claude-code") == []
    assert agent_runtime_hosts(agent_name="codex") == []
    assert agent_runtime_hosts(agent_name=None) == []


def test_a_custom_endpoint_is_added_alongside_the_default():
    # Added rather than substituted: a fallback to the production endpoint
    # must not be silently un-allowlisted by pointing at staging.
    assert agent_runtime_hosts(
        agent_name="tbh", agent_kwargs={"base_url": "https://staging.meta.ai/v1"}
    ) == ["api.meta.ai", "staging.meta.ai"]
    assert agent_runtime_hosts(
        agent_name="tbh", agent_env={"TBH_BASE_URL": "https://staging.meta.ai"}
    ) == ["api.meta.ai", "staging.meta.ai"]
    assert agent_runtime_hosts(
        agent_name="tbh",
        agent_kwargs={"extra_env": {"TBH_BASE_URL": "https://staging.meta.ai"}},
    ) == ["api.meta.ai", "staging.meta.ai"]


def test_an_oddish_wrapper_import_path_still_resolves_its_agent():
    # Wrappers null the name and set an import path instead.
    assert agent_runtime_hosts(
        agent_name=None, import_path="oddish.workers.agents.tbh:Tbh"
    ) == ["api.meta.ai"]
