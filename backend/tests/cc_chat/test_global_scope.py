from api.services.cc_chat.claude_md import render_global_claude_md


def test_global_claude_md_documents_cli_and_discipline():
    md = render_global_claude_md(org_id="org_42")
    assert "oddish-query tasks search" in md
    assert "oddish-query trials logs" in md
    # shallow-by-default steering
    assert "search" in md.lower()
    assert "one trial at a time" in md.lower()
