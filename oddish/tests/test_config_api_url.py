"""Unit tests for deriving the deployed backend API base URL."""

from oddish.config import (
    DEFAULT_API_URL,
    PREVIEW_URL_TEMPLATE,
    STAGING_API_URL,
    api_base_url_for_modal_app,
)


def test_prod_app_name_resolves_to_default():
    assert api_base_url_for_modal_app("oddish") == DEFAULT_API_URL


def test_staging_app_name_resolves_to_staging():
    # The arm whose absence sent staging's QA/audit sandboxes to prod with
    # staging-minted keys (every fetch 401'd as "session credential expired").
    assert api_base_url_for_modal_app("oddish-staging") == STAGING_API_URL


def test_pr_preview_app_name_resolves_to_preview_url():
    assert api_base_url_for_modal_app("oddish-pr-331") == PREVIEW_URL_TEMPLATE.format(
        n="331"
    )


def test_empty_or_unset_app_name_returns_empty(monkeypatch):
    # Explicit empty -> local/unknown (caller fails fast rather than hitting prod).
    assert api_base_url_for_modal_app("") == ""
    # None + MODAL_APP_NAME unset -> empty.
    monkeypatch.delenv("MODAL_APP_NAME", raising=False)
    assert api_base_url_for_modal_app() == ""


def test_reads_modal_app_name_from_env_when_not_passed(monkeypatch):
    monkeypatch.setenv("MODAL_APP_NAME", "oddish-pr-7")
    assert api_base_url_for_modal_app() == PREVIEW_URL_TEMPLATE.format(n="7")


def test_unknown_app_names_fail_closed():
    """An unrecognized app identity must resolve to nothing, never to prod:
    the caller (probe/QA cred injection) then fails at trial start with an
    actionable error instead of silently querying another environment's API.
    Before this rule, every unknown name fell through to the prod URL."""
    assert api_base_url_for_modal_app("oddish-pr-foo") == ""
    assert api_base_url_for_modal_app("oddish-dev") == ""
    assert api_base_url_for_modal_app("someone-elses-fork") == ""


def test_cli_config_reexports_constants():
    # Backward-compat: the CLI module still exposes the moved constants.
    from oddish.cli import config as cli_config

    assert cli_config.DEFAULT_API_URL == DEFAULT_API_URL
    assert cli_config.PREVIEW_URL_TEMPLATE == PREVIEW_URL_TEMPLATE
