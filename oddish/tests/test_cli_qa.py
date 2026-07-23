import pytest
import typer

from oddish.cli.qa import _load_variants


def test_load_variants_resolves_active_and_pinned_references():
    assert _load_variants(["oracle-check", "degenerate-check@2"]) == [
        {"kind": "oracle-check"},
        {"kind": "degenerate-check", "version": 2},
    ]


@pytest.mark.parametrize("reference", ["@2", "prompt@nope", "prompt@0"])
def test_load_variants_rejects_invalid_references(reference):
    with pytest.raises(typer.BadParameter):
        _load_variants([reference])
