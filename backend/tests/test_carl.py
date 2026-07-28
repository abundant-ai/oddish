from __future__ import annotations

import sys
import types

import carl
import pytest


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


def test_partial_overflow_delivery_reports_failure(monkeypatch):
    updates = []
    monkeypatch.setattr(carl, "_update", lambda *args: updates.append(args))
    monkeypatch.setattr(
        carl, "_post", lambda *_args: (_ for _ in ()).throw(RuntimeError("Slack down"))
    )

    assert (
        carl._deliver("C123", "100.2", "100.1", "x" * (carl._MAX_SLACK + 1))
        == "partial"
    )
    assert updates[-1][2].endswith(carl._PARTIAL_SUFFIX)
    assert updates[-1][2].startswith("x")


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
    assert "users" in carl_tools._validate_sql("select id from users")
    assert "Wildcard" in carl_tools._validate_sql("select * from trials")
    assert "harbor_config" in carl_tools._validate_sql(
        "select harbor_config from trials"
    )
    assert "Whole-row" in carl_tools._validate_sql(
        "select to_jsonb(trials) from trials"
    )
    assert (
        carl_tools._validate_sql(
            "with recent as (select id from trials) select id from recent"
        )
        is None
    )
    assert "users" in carl_tools._validate_sql(
        "with users as (select id from users) select id from users"
    )
    assert "users" in carl_tools._validate_sql(
        "with first as (select id from users), users as (select id from trials) "
        "select id from first"
    )
    assert "pg_read_file" in carl_tools._validate_sql("select pg_read_file('/etc/passwd')")
    assert "pg_read_file" in carl_tools._validate_sql(
        "select coalesce(pg_read_file('/etc/passwd'), '')"
    )
    assert "users" in carl_tools._validate_sql(
        "select coalesce((select count(*) from users), 0)"
    )


@pytest.mark.asyncio
async def test_read_only_sql_sets_transaction_options_separately(monkeypatch):
    import carl_tools

    class Transaction:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Cursor:
        async def fetch(self, _limit):
            return []

    class Connection:
        def __init__(self):
            self.commands = []

        def transaction(self, *, readonly):
            assert readonly
            return Transaction()

        async def execute(self, command):
            self.commands.append(command)

        async def cursor(self, _query):
            return Cursor()

        async def close(self):
            return None

    connection = Connection()

    async def connect(*_args, **_kwargs):
        return connection

    monkeypatch.setenv("ODDISH_DATABASE_URL_RO", "postgresql://carl@example/oddish")
    monkeypatch.setattr(carl_tools.asyncpg, "connect", connect)

    result = await carl_tools.oddish_sql({"query": "select id from trials limit 1"})

    assert result["content"][0]["text"] == "_(0 rows returned)_"
    assert connection.commands == [
        "SET LOCAL statement_timeout = 15000",
        "SET LOCAL search_path = public, information_schema",
    ]
