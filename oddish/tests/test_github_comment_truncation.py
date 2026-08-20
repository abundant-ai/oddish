"""``fit_comment_body`` keeps comments under GitHub's 65,536-character cap
(oversized bodies got 422 Unprocessable Entity) while preserving the head,
where the ``<!-- oddish-… -->`` marker that ``find_oddish_comment`` matches
on lives.
"""

from oddish.integrations.github.client import (
    _MAX_COMMENT_CHARS,
    _TRUNCATION_NOTICE,
    fit_comment_body,
)

MARKER = "<!-- oddish-experiment-results -->"


def test_short_body_unchanged():
    body = f"{MARKER}\nAll good."
    assert fit_comment_body(body) == body


def test_exact_cap_untouched():
    body = "y" * _MAX_COMMENT_CHARS
    assert fit_comment_body(body) == body


def test_oversized_body_cut_to_cap_keeping_marker_and_notice():
    body = MARKER + "\n" + "x" * (2 * _MAX_COMMENT_CHARS)
    fitted = fit_comment_body(body)
    assert len(fitted) == _MAX_COMMENT_CHARS
    assert fitted.startswith(MARKER)
    assert fitted.endswith(_TRUNCATION_NOTICE)
