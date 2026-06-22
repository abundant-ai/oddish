from oddish.config import (
    HARBOR_DEFAULT_SHA,
    HARBOR_DEFAULT_SOURCE,
    Settings,
    resolve_harbor_layers,
)

FORK = HARBOR_DEFAULT_SOURCE


def test_no_layer_set_returns_locked_default():
    assert resolve_harbor_layers(flag=None, env=None, manifest=None) == (
        HARBOR_DEFAULT_SOURCE,
        HARBOR_DEFAULT_SHA,
    )


def test_flag_wins_over_env_and_manifest_atomically():
    # flag sets only a ref; env sets a different source — must NOT merge.
    out = resolve_harbor_layers(
        flag="feature/from-flag",
        env="other/repo@from-env",
        manifest={"source": "https://github.com/x/y", "ref": "from-manifest"},
    )
    assert out == (FORK, "feature/from-flag")


def test_env_wins_over_manifest():
    out = resolve_harbor_layers(
        flag=None,
        env="dot-agi/harbor@from-env",
        manifest={"source": "https://github.com/x/y", "ref": "from-manifest"},
    )
    assert out == ("https://github.com/dot-agi/harbor", "from-env")


def test_manifest_structured_table_used_when_no_flag_or_env():
    out = resolve_harbor_layers(
        flag=None,
        env=None,
        manifest={"source": "https://github.com/dot-agi/harbor", "ref": "v9@rc1"},
    )
    # structured table is the literal-'@' escape hatch: ref kept verbatim.
    assert out == ("https://github.com/dot-agi/harbor", "v9@rc1")


def test_settings_expose_harbor_envs():
    s = Settings(
        harbor="main",
        harbor_allowed_sources="https://github.com/x/*",
    )
    assert s.harbor == "main"
    assert s.harbor_allowed_sources == "https://github.com/x/*"
