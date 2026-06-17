"""Unit tests for deriving the deployed backend API base URL."""

from oddish.config import (
    DEFAULT_API_URL,
    PREVIEW_URL_TEMPLATE,
    api_base_url_for_modal_app,
)


def test_prod_app_name_resolves_to_default():
    assert api_base_url_for_modal_app("oddish") == DEFAULT_API_URL


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


def test_non_numeric_pr_suffix_falls_back_to_default():
    assert api_base_url_for_modal_app("oddish-pr-foo") == DEFAULT_API_URL


def test_cli_config_reexports_constants():
    # Backward-compat: the CLI module still exposes the moved constants.
    from oddish.cli import config as cli_config

    assert cli_config.DEFAULT_API_URL == DEFAULT_API_URL
    assert cli_config.PREVIEW_URL_TEMPLATE == PREVIEW_URL_TEMPLATE
