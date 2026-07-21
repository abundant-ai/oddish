from oddish.workers.harbor.model_hosts import outbound_hosts_for_model


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


def test_outbound_hosts_prefer_routed_base_url_env():
    hosts = outbound_hosts_for_model(
        "claude-sonnet-4",  # no useful provider prefix
        agent_env={"ANTHROPIC_BASE_URL": "https://api.fireworks.ai/inference"},
    )
    assert hosts == ["api.fireworks.ai"]


def test_outbound_hosts_bedrock_model_ids():
    hosts = outbound_hosts_for_model(
        "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
    )
    assert "bedrock-runtime.us-east-1.amazonaws.com" in hosts
    assert "sts.amazonaws.com" in hosts
