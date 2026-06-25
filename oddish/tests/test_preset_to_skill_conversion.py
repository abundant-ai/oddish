from oddish.core.probe.preset_migration import preset_row_to_skill


def test_converts_preset_to_skill_with_synthesized_skill_md():
    preset = {
        "id": "cheat-detector",
        "org_id": None,
        "name": "Cheat detector",
        "operator_prompt": "You are a security researcher. Find a cheat.\nMore detail.",
        "result_focus": "Did a cheat succeed?",
        "evaluation_metric": "ratio",
        "is_seed": True,
        "created_at": "2026-04-30T00:00:00+00:00",
        "updated_at": "2026-04-30T00:00:00+00:00",
        "deleted_at": None,
    }
    skill, skill_md = preset_row_to_skill(preset)

    assert skill["id"] == "cheat-detector"          # id preserved
    assert skill["name"] == "Cheat detector"
    assert skill["operator_prompt"] == preset["operator_prompt"]
    assert skill["result_focus"] == "Did a cheat succeed?"
    assert skill["evaluation_metric"] == "ratio"
    assert skill["is_seed"] is True
    # description is the first line of the prompt, truncated
    assert skill["description"].startswith("You are a security researcher")
    # SKILL.md is valid frontmatter with name + description, body = prompt
    assert skill_md["skill_id"] == "cheat-detector"
    assert skill_md["relative_path"] == "SKILL.md"
    assert skill_md["content"].startswith("---\n")
    assert "name: Cheat detector" in skill_md["content"]
    assert preset["operator_prompt"] in skill_md["content"]


def test_description_truncated_to_255():
    preset = {
        "id": "x", "org_id": None, "name": "Long",
        "operator_prompt": "A" * 400, "result_focus": None,
        "evaluation_metric": None, "is_seed": False,
        "created_at": "2026-04-30T00:00:00+00:00",
        "updated_at": "2026-04-30T00:00:00+00:00", "deleted_at": None,
    }
    skill, _ = preset_row_to_skill(preset)
    assert len(skill["description"]) <= 255
