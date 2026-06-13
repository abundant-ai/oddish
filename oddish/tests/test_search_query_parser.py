"""Unit tests for the free-text search grammar (`parse_search_query`).

Pure parser tests — no database. The grammar: whitespace-separated terms are
AND'd, `"quoted text"` matches contiguously (spaces preserved), a leading `-`
excludes a term or phrase, uppercase OR/AND/NOT between terms are operators
(lowercase and quoted forms are literals), an unterminated quote runs to end
of string. `include` is an AND of OR-groups.
"""

from oddish.core.helpers import parse_search_query


def test_single_token():
    assert parse_search_query("murmur3").include == (("murmur3",),)


def test_multiple_tokens_are_anded_groups():
    terms = parse_search_query("murmur x86 conformance")
    assert terms.include == (("murmur",), ("x86",), ("conformance",))
    assert terms.exclude == ()


def test_quoted_phrase_keeps_spaces():
    terms = parse_search_query('"x86-32 conformance" extra')
    assert terms.include == (("x86-32 conformance",), ("extra",))


def test_negated_term_and_phrase():
    terms = parse_search_query('-no-skill -"rel 2242"')
    assert terms.include == ()
    assert terms.exclude == ("no-skill", "rel 2242")


def test_or_groups_adjacent_terms():
    terms = parse_search_query("murmur OR polygon cookie")
    assert terms.include == (("murmur", "polygon"), ("cookie",))


def test_or_chains_into_one_group():
    terms = parse_search_query("a OR b OR c")
    assert terms.include == (("a", "b", "c"),)


def test_or_joins_phrases():
    terms = parse_search_query('"a b" OR c')
    assert terms.include == (("a b", "c"),)


def test_uppercase_not_excludes_next_term():
    terms = parse_search_query('task NOT older NOT "a b"')
    assert terms.include == (("task",),)
    assert terms.exclude == ("older", "a b")


def test_uppercase_and_is_noop():
    terms = parse_search_query("a AND b")
    assert terms.include == (("a",), ("b",))


def test_lowercase_operators_are_literal_terms():
    terms = parse_search_query("not really or related and stuff")
    assert terms.include == (
        ("not",),
        ("really",),
        ("or",),
        ("related",),
        ("and",),
        ("stuff",),
    )
    assert terms.exclude == ()


def test_quoted_operators_are_literal_terms():
    terms = parse_search_query('"OR" "NOT"')
    assert terms.include == (("OR",), ("NOT",))


def test_dangling_operators_are_dropped():
    assert parse_search_query("OR a").include == (("a",),)
    assert parse_search_query("a OR").include == (("a",),)
    assert not parse_search_query("NOT")
    assert not parse_search_query("OR AND")


def test_or_does_not_reach_over_an_exclusion():
    terms = parse_search_query("a -b OR c")
    assert terms.include == (("a",), ("c",))
    assert terms.exclude == ("b",)


def test_or_before_exclusion_is_dangling():
    terms = parse_search_query("a OR -b")
    assert terms.include == (("a",),)
    assert terms.exclude == ("b",)


def test_negated_operator_token_is_a_literal_exclusion():
    terms = parse_search_query("-OR")
    assert terms.include == ()
    assert terms.exclude == ("OR",)


def test_unterminated_quote_runs_to_end():
    terms = parse_search_query('alpha "beta gamma')
    assert terms.include == (("alpha",), ("beta gamma",))


def test_quoted_literal_dash_is_included_not_excluded():
    terms = parse_search_query('"-no-skill"')
    assert terms.include == (("-no-skill",),)
    assert terms.exclude == ()


def test_bare_dash_is_a_literal_token():
    terms = parse_search_query("-")
    assert terms.include == (("-",),)
    assert terms.exclude == ()


def test_empty_inputs_yield_no_terms():
    assert not parse_search_query("")
    assert not parse_search_query("   ")
    assert not parse_search_query('""')
    assert not parse_search_query('-""')


def test_quote_inside_bare_word_stays_literal():
    terms = parse_search_query('foo"bar')
    assert terms.include == (('foo"bar',),)


def test_like_metacharacters_pass_through_for_caller_escaping():
    terms = parse_search_query("100% under_score")
    assert terms.include == (("100%",), ("under_score",))


def test_term_cap():
    terms = parse_search_query(" ".join(f"t{i}" for i in range(40)))
    assert sum(len(group) for group in terms.include) == 16


def test_truthiness():
    assert parse_search_query("x")
    assert parse_search_query("-x")
    assert not parse_search_query("")
