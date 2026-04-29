from datetime import datetime, timedelta, timezone

from api.services.cc_chat.sessions import SessionRegistry, SessionState


def _now():
    return datetime.now(timezone.utc)


def test_register_and_get():
    reg = SessionRegistry()
    state = SessionState(
        session_id="sid-1",
        experiment_id="exp-1",
        org_id="org-1",
        sandbox_id="sbx-1",
        daytona_session_id="cc",
        created_at=_now(),
        last_activity=_now(),
        claude_session_id=None,
    )
    reg.put(state)
    assert reg.get("sid-1") is state
    assert reg.get("missing") is None


def test_pop_returns_and_removes():
    reg = SessionRegistry()
    state = SessionState(
        session_id="sid-1",
        experiment_id="exp-1",
        org_id="org-1",
        sandbox_id="sbx-1",
        daytona_session_id="cc",
        created_at=_now(),
        last_activity=_now(),
        claude_session_id=None,
    )
    reg.put(state)
    assert reg.pop("sid-1") is state
    assert reg.pop("sid-1") is None


def test_idle_returns_sessions_older_than_threshold():
    reg = SessionRegistry()
    now = _now()
    fresh = SessionState(
        session_id="fresh",
        experiment_id="e",
        org_id="o",
        sandbox_id="s",
        daytona_session_id="cc",
        created_at=now,
        last_activity=now,
        claude_session_id=None,
    )
    stale = SessionState(
        session_id="stale",
        experiment_id="e",
        org_id="o",
        sandbox_id="s",
        daytona_session_id="cc",
        created_at=now - timedelta(hours=1),
        last_activity=now - timedelta(minutes=45),
        claude_session_id=None,
    )
    reg.put(fresh)
    reg.put(stale)
    idle = list(reg.idle(now=now, max_idle=timedelta(minutes=30)))
    assert [s.session_id for s in idle] == ["stale"]


def test_touch_updates_last_activity():
    reg = SessionRegistry()
    now = _now()
    state = SessionState(
        session_id="sid",
        experiment_id="e",
        org_id="o",
        sandbox_id="s",
        daytona_session_id="cc",
        created_at=now - timedelta(hours=1),
        last_activity=now - timedelta(hours=1),
        claude_session_id=None,
    )
    reg.put(state)
    reg.touch("sid", now=now)
    assert reg.get("sid").last_activity == now
