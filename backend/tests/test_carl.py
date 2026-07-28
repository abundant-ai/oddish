from __future__ import annotations

import sys
import types

import carl


def _mention():
    event = {
        "type": "app_mention",
        "channel": "C123",
        "user": "UASKER",
        "ts": "100.1",
        "text": "<@UCARL> what is queue health?",
    }
    return {
        "event_id": "Ev123",
        "team_id": "T123",
        "authorizations": [{"is_bot": True, "user_id": "UCARL"}],
        "event": event,
    }


def test_dispatches_allowed_mention_to_carl_answer(monkeypatch):
    spawned = []
    monkeypatch.setenv("ODDISH_CARL_ALLOWED_USERS", "UASKER")
    monkeypatch.setenv("ODDISH_CARL_ALLOWED_CHANNELS", "C123")
    monkeypatch.setattr(carl, "_claim_event", lambda _event_id: True)
    monkeypatch.setattr(carl, "_spawn_answer", lambda *args: spawned.append(args))

    carl.dispatch_app_mention(_mention())

    assert spawned == [("C123", "100.1", "what is queue health?", "UASKER", "Ev123")]


def test_rejects_user_outside_allowlist(monkeypatch):
    notices = []
    monkeypatch.setenv("ODDISH_CARL_ALLOWED_USERS", "UOTHER")
    monkeypatch.delenv("ODDISH_CARL_ALLOWED_CHANNELS", raising=False)
    monkeypatch.setattr(carl, "_claim_event", lambda _event_id: True)
    monkeypatch.setattr(carl, "_spawn_answer", lambda *_args: None)
    monkeypatch.setattr(carl, "_notify", lambda *args: notices.append(args))

    carl.dispatch_app_mention(_mention())

    assert notices == [
        ("C123", "100.1", "Sorry, you aren't authorized to ask Carl.", "Ev123")
    ]


def test_duplicate_event_is_ignored(monkeypatch):
    monkeypatch.setenv("ODDISH_CARL_ALLOWED_USERS", "UASKER")
    monkeypatch.setattr(carl, "_claim_event", lambda _event_id: False)
    monkeypatch.setattr(
        carl,
        "_spawn_answer",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )
    monkeypatch.setattr(
        carl,
        "_notify",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not reply")),
    )

    carl.dispatch_app_mention(_mention())


def test_bot_token_reuses_existing_carl_credentials(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ODDISH_SLACK_UNFURL_BOT_TOKEN", "xoxb-carl")
    monkeypatch.setenv("SLACK_ALERT_BOT_TOKEN", "xoxb-alert")

    assert carl._bot_token() == "xoxb-carl"


def test_read_only_sql_guard_rejects_writes_and_private_tables(monkeypatch):
    sdk = types.ModuleType("claude_agent_sdk")

    def tool(name, _description, _schema):
        def decorate(function):
            function.name = name
            return function

        return decorate

    sdk.tool = tool
    sdk.create_sdk_mcp_server = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)

    import carl_tools

    assert carl_tools._validate_sql("select count(*) from trials") is None
    assert "InsertStmt" in carl_tools._validate_sql(
        "with changed as (insert into trials(id) values ('x') returning id) "
        "select * from changed"
    )
    assert "users" in carl_tools._validate_sql("select * from users")
    assert "pg_read_file" in carl_tools._validate_sql("select pg_read_file('/etc/passwd')")
