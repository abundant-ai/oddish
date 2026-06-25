from oddish.core.probe import auto_probe


def test_default_probe_skill_id_is_a_seed_skill():
    # The auto-probe must reference a skill, not a preset.
    assert hasattr(auto_probe, "DEFAULT_PROBE_SKILL_ID")
    assert not hasattr(auto_probe, "DEFAULT_PROBE_PRESET_ID")


def test_auto_probe_does_not_import_probe_preset_model():
    import inspect
    src = inspect.getsource(auto_probe)
    assert "ProbePresetModel" not in src
