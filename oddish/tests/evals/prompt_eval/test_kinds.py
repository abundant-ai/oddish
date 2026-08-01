from oddish.evals.prompt_eval.kinds import (
    PROMPT_KIND_SPECS,
    classify_changed_paths,
)


def test_prompt_change_maps_to_kind():
    changes = classify_changed_paths(
        ["oddish/src/oddish/analyze/classify_prompt.txt", "oddish/README.md"]
    )
    assert changes.prompt_changed == {"QA_POST_TRIAL"}
    assert changes.affected_kinds() == ["QA_POST_TRIAL"]


def test_all_prompt_files_map():
    changes = classify_changed_paths(
        [spec.prompt_path for spec in PROMPT_KIND_SPECS.values()]
    )
    assert changes.prompt_changed == set(PROMPT_KIND_SPECS)


def test_goldens_change_maps_to_kind():
    changes = classify_changed_paths(
        ["evals/prompt-goldens/QA_PRE_TRIAL/cases/foo/case.yaml"]
    )
    assert changes.prompt_changed == set()
    assert changes.goldens_changed == {"QA_PRE_TRIAL"}


def test_config_change_sweeps_all_kinds():
    changes = classify_changed_paths(["evals/prompt-goldens/config.yaml"])
    assert changes.config_changed
    assert set(changes.affected_kinds()) == set(PROMPT_KIND_SPECS)


def test_unrelated_paths_are_ignored():
    changes = classify_changed_paths(
        ["frontend/src/app/page.tsx", "evals/prompt-goldens/README.md", ""]
    )
    assert changes.affected_kinds() == []
