from oddish.db import SkillModel, SkillFileModel


def test_skill_model_has_directive_columns():
    skill = SkillModel(
        name="x",
        description="d",
        operator_prompt="do the thing",
        result_focus="what happened?",
        evaluation_metric="result_focus",
        files=[SkillFileModel(relative_path="SKILL.md", content="---\nname: x\ndescription: d\n---\nbody")],
    )
    assert skill.operator_prompt == "do the thing"
    assert skill.result_focus == "what happened?"
    assert skill.evaluation_metric == "result_focus"


def test_skill_model_directive_columns_default_none():
    skill = SkillModel(name="y", description="d")
    assert skill.operator_prompt is None
    assert skill.result_focus is None
    assert skill.evaluation_metric is None
