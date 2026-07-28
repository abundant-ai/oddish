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


@pytest.mark.asyncio
async def test_answer_startup_failure_releases_claim_and_replies(monkeypatch):
    import carl_agent

    async def fail_to_start(*_args):
        raise ImportError("missing agent dependency")

    released = []
    posts = []
    monkeypatch.setattr(carl_agent, "_carl_answer_impl", fail_to_start)
    monkeypatch.setattr(carl_agent, "_release_event", released.append)
    monkeypatch.setattr(carl_agent, "_post", lambda *args: posts.append(args))

    await carl_agent._run_carl_answer(
        "C123", "100.1", "what is queue health?", "UASKER", "Ev123"
    )

    assert released == ["Ev123"]
    assert posts == [
        (
            "C123",
            "100.1",
            ":warning: I couldn't start that answer. Please mention me again.",
        )
    ]


def test_empty_answer_failure_releases_claim_when_status_update_fails(monkeypatch):
    import carl_agent

    released = []
    monkeypatch.setattr(
        carl_agent,
        "_update",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("Slack down")),
    )
    monkeypatch.setattr(carl_agent, "_release_event", released.append)

    carl_agent._finish_empty_failure(
        "C123", "100.2", ":warning:", "I hit an error", "Ev123"
    )

    assert released == ["Ev123"]


@pytest.mark.parametrize(
    ("turn_limit", "budget_limit", "reason"),
    [
        (True, False, "step limit"),
        (False, True, "$1.00 cost limit"),
    ],
)
def test_limit_answer_preserves_partial_text(turn_limit, budget_limit, reason):
    import carl_agent

    answer = carl_agent._limited_answer(
        "",
        "Partial analysis",
        hit_turn_limit=turn_limit,
        hit_budget_limit=budget_limit,
        budget=1.0,
    )

    assert answer.startswith("Partial analysis\n\n")
    assert reason in answer
    assert "this is what I had so far" in answer


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
    assert "public_token" in carl_tools._validate_sql(
        "select public_token from experiments"
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


def test_user_costs_include_task_and_model_breakdowns():
    import carl_tools

    result = carl_tools._format_user_costs(
        {
            "name": "Ada",
            "window_days": 7,
            "totals": {"cost_usd": 4.5, "trial_count": 3, "task_count": 1},
            "tasks": [
                {"task_name": "proof", "cost_usd": 4.5, "trial_count": 3}
            ],
            "series_by_model": {
                "keys": [
                    {"key": "claude-opus-4-8", "label": "Claude Opus 4.8"},
                    {"key": "grok", "label": "Grok"},
                ],
                "buckets": [
                    {"costs": {"claude-opus-4-8": 1.25, "grok": 2.0}},
                    {"costs": {"claude-opus-4-8": 1.25}},
                ],
            },
        },
        "U123",
    )
    text = result["content"][0]["text"]

    assert "*Top tasks*\n• proof: $4.50, 3 trials" in text
    assert "*Top models*\n• Claude Opus 4.8: $2.50\n• Grok: $2.00" in text


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
