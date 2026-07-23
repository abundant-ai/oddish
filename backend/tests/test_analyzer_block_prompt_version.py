from oddish.blocks.analyzer.analyzer_block import _block_row_kwargs


def test_block_row_includes_prompt_version():
    kwargs = _block_row_kwargs(
        block_metadata={"prompt_key": "pre_trial_qa", "prompt_version": 3, "model": "m"}
    )
    assert kwargs["prompt_key"] == "pre_trial_qa"
    assert kwargs["prompt_version"] == 3
