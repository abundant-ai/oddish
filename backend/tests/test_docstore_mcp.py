"""Tests for the docstore MCP server's pure helpers (formatting + scope)."""

from oddish.db import DocumentModel
from oddish.mcp.docstore_server import _card_dict, _doc_detail, _scoped_org_id


def test_card_dict_shape():
    d = DocumentModel(
        id="abc", org_id="o", title="T", summary="S", tags=["x"], source_type="paste"
    )
    assert _card_dict(d) == {"id": "abc", "title": "T", "summary": "S", "tags": ["x"]}


def test_doc_detail_includes_digest_not_full_text():
    d = DocumentModel(
        id="abc",
        org_id="o",
        title="T",
        summary="S",
        digest_text="DIG",
        tags=["x"],
        source_type="link",
        source_url="http://e",
        content_text="FULL",
    )
    detail = _doc_detail(d)
    assert detail["digest"] == "DIG"
    assert detail["source_url"] == "http://e"
    # get_document returns the digest, never the full extracted text.
    assert "FULL" not in detail.values()


def test_scoped_org_id_reads_env(monkeypatch):
    monkeypatch.setenv("ODDISH_DOCSTORE_ORG_ID", "org_42")
    assert _scoped_org_id() == "org_42"
    monkeypatch.delenv("ODDISH_DOCSTORE_ORG_ID", raising=False)
    assert _scoped_org_id() is None
