"""Unit tests for the free-text search grammar (`parse_search_query`).

Pure parser tests — no database. The grammar: whitespace-separated terms are
AND'd, `"quoted text"` matches contiguously (spaces preserved), a leading `-`
excludes a term or phrase, an unterminated quote runs to end of string.
"""

from oddish.core.helpers import parse_search_query


def test_single_token():
    assert parse_search_query("murmur3").include == ("murmur3",)


def test_multiple_tokens_are_separate_needles():
    terms = parse_search_query("murmur x86 conformance")
    assert terms.include == ("murmur", "x86", "conformance")
    assert terms.exclude == ()


def test_quoted_phrase_keeps_spaces():
    terms = parse_search_query('"x86-32 conformance" extra')
    assert terms.include == ("x86-32 conformance", "extra")


def test_negated_term_and_phrase():
    terms = parse_search_query('-no-skill -"rel 2242"')
    assert terms.include == ()
    assert terms.exclude == ("no-skill", "rel 2242")


def test_unterminated_quote_runs_to_end():
    terms = parse_search_query('alpha "beta gamma')
    assert terms.include == ("alpha", "beta gamma")


def test_quoted_literal_dash_is_included_not_excluded():
    terms = parse_search_query('"-no-skill"')
    assert terms.include == ("-no-skill",)
    assert terms.exclude == ()


def test_bare_dash_is_a_literal_token():
    terms = parse_search_query("-")
    assert terms.include == ("-",)
    assert terms.exclude == ()


def test_empty_inputs_yield_no_terms():
    assert not parse_search_query("")
    assert not parse_search_query("   ")
    assert not parse_search_query('""')
    assert not parse_search_query('-""')


def test_quote_inside_bare_word_stays_literal():
    terms = parse_search_query('foo"bar')
    assert terms.include == ('foo"bar',)


def test_like_metacharacters_pass_through_for_caller_escaping():
    terms = parse_search_query("100% under_score")
    assert terms.include == ("100%", "under_score")


def test_term_cap():
    terms = parse_search_query(" ".join(f"t{i}" for i in range(40)))
    assert len(terms.include) == 16


def test_truthiness():
    assert parse_search_query("x")
    assert parse_search_query("-x")
    assert not parse_search_query("")
