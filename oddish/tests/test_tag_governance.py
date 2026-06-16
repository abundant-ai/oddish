"""Governance tests: policy upsert + 10-tag cap."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _run(coro):
    return asyncio.run(coro)


class _StubResult:
    def __init__(self, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar(self):
        return self._scalar

    def all(self):
        return self._rows

    @property
    def rowcount(self):
        return 1


class _FakeSession:
    def __init__(self, scalar=None):
        self.scalar_value = scalar
        self.executed: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), dict(params or {})))
        return _StubResult(scalar=self.scalar_value)

    async def scalar(self, stmt, params=None):
        self.executed.append((str(stmt), dict(params or {})))
        return self.scalar_value

    async def flush(self):
        pass


def test_get_or_create_tag_policy_creates_default_row(monkeypatch):
    from oddish.core import tag_policies_core

    session = _FakeSession(scalar=None)
    _run(tag_policies_core.get_or_create_tag_policy(session, org_id="org-1"))
    assert any("INSERT INTO tag_policies" in s for s, _ in session.executed)


def test_count_direct_active_tags_at_target_scope():
    from oddish.core import tag_policies_core

    session = _FakeSession(scalar=7)
    n = _run(
        tag_policies_core.count_direct_active_tags(
            session, scope="TASK", target_id="task-1", org_id="org-1"
        )
    )
    assert n == 7


def test_assert_under_tag_cap_raises_when_over_limit():
    from oddish.core import tag_policies_core

    session = _FakeSession(scalar=10)
    import pytest

    with pytest.raises(tag_policies_core.TagCapExceededError):
        _run(
            tag_policies_core.assert_under_tag_cap(
                session,
                scope="TASK",
                target_id="task-1",
                org_id="org-1",
                max_tags_per_entity=10,
            )
        )


def test_create_tag_core_rejects_profanity_in_enforce_mode(monkeypatch):
    from oddish.core import tags_core
    from oddish.core import tag_profanity

    def _fake_check(text, policy):
        return tag_profanity.ProfanityResult(allowed=False, hits=["x"], mode="ENFORCE")

    monkeypatch.setattr(tag_profanity, "check_tag_text", _fake_check)
    monkeypatch.setattr(tags_core, "check_tag_text", _fake_check)

    session = _FakeSession()

    import pytest

    with pytest.raises(tags_core.TagProfanityError):
        _run(
            tags_core.create_tag_core(
                session,
                key="badword",
                value=None,
                org_id="org-1",
                actor_user_id="u-1",
                policy={
                    "profanity_mode": "ENFORCE",
                    "name_max_len": 64,
                    "name_charset": "[a-z0-9._-]",
                    "reserved_prefixes": [],
                    "who_can_create": "ANY_MEMBER",
                    "profanity_allowlist": [],
                    "profanity_denylist": [],
                },
                is_admin=False,
            )
        )


def test_create_tag_core_rejects_reserved_prefix_for_non_admin(monkeypatch):
    from oddish.core import tags_core

    monkeypatch.setattr(
        tags_core,
        "check_tag_text",
        lambda t, p: __import__("types").SimpleNamespace(
            allowed=True, hits=[], mode="ENFORCE"
        ),
    )

    session = _FakeSession()

    import pytest

    with pytest.raises(tags_core.TagReservedNamespaceError):
        _run(
            tags_core.create_tag_core(
                session,
                key="system:foo",
                value=None,
                org_id="org-1",
                actor_user_id="u-1",
                policy={
                    "profanity_mode": "OFF",
                    "name_max_len": 64,
                    "name_charset": "[a-z0-9._-]",
                    "reserved_prefixes": ["system:"],
                    "who_can_create": "ANY_MEMBER",
                    "profanity_allowlist": [],
                    "profanity_denylist": [],
                },
                is_admin=False,
            )
        )


def test_create_tag_core_admin_only_policy_blocks_member(monkeypatch):
    from oddish.core import tags_core

    monkeypatch.setattr(
        tags_core,
        "check_tag_text",
        lambda t, p: __import__("types").SimpleNamespace(
            allowed=True, hits=[], mode="OFF"
        ),
    )

    session = _FakeSession()

    import pytest

    with pytest.raises(tags_core.TagPolicyError):
        _run(
            tags_core.create_tag_core(
                session,
                key="my-tag",
                value=None,
                org_id="org-1",
                actor_user_id="u-1",
                policy={
                    "profanity_mode": "OFF",
                    "name_max_len": 64,
                    "name_charset": "[a-z0-9._-]",
                    "reserved_prefixes": [],
                    "who_can_create": "ADMIN_ONLY",
                    "profanity_allowlist": [],
                    "profanity_denylist": [],
                },
                is_admin=False,
            )
        )


def test_create_tag_core_rejects_invalid_visibility():
    from oddish.core import tags_core

    session = _FakeSession()

    import pytest

    # The visibility guard runs right after the ADMIN_ONLY check and BEFORE any
    # session use, so 'ORG' (a saved-filter visibility, not a tag visibility) is
    # rejected as TagNameError without ever touching the DB. who_can_create is
    # ANY_MEMBER so the ADMIN_ONLY gate passes and we reach the visibility guard.
    with pytest.raises(tags_core.TagNameError):
        _run(
            tags_core.create_tag_core(
                session,
                key="my-tag",
                value=None,
                org_id="org-1",
                actor_user_id="u-1",
                policy={"who_can_create": "ANY_MEMBER"},
                is_admin=False,
                visibility="ORG",
            )
        )
    # Guard fires before any DB access; 'PRIVATE'/'PUBLIC' are the only accepted
    # values and would pass this gate.
    assert session.executed == []


def test_set_tag_visibility_core_writes_update_and_event(monkeypatch):
    from oddish.core import tags_core

    session = _FakeSession()
    _run(
        tags_core.set_tag_visibility_core(
            session,
            tag_id="tag-1",
            org_id="org-1",
            actor_user_id="u-1",
            new_visibility="PUBLIC",
            expected_row_version=1,
        )
    )
    assert any(
        "UPDATE tags" in s and "visibility = :new_visibility" in s
        for s, _ in session.executed
    )
    assert any(
        "INSERT INTO tag_events" in s
        and "SET_VISIBILITY" in str(params).upper()
        or "SET_VISIBILITY" in s
        for s, params in session.executed
    )


def test_archive_tag_core_optimistic_concurrency_zero_rows_raises(monkeypatch):
    from oddish.core import tags_core

    class _RowCountSession(_FakeSession):
        async def execute(self, stmt, params=None):
            self.executed.append((str(stmt), dict(params or {})))

            class _R:
                @property
                def rowcount(self_inner):
                    return 0

            return _R()

    import pytest

    s = _RowCountSession()
    with pytest.raises(tags_core.TagConcurrencyError):
        _run(
            tags_core.archive_tag_core(
                s,
                tag_id="tag-1",
                org_id="org-1",
                actor_user_id="u-1",
                expected_row_version=42,
            )
        )


def test_merge_tag_core_rejects_chained_merge(monkeypatch):
    from oddish.core import tags_core

    class _ChainSession(_FakeSession):
        async def execute(self, stmt, params=None):
            self.executed.append((str(stmt), dict(params or {})))
            # The new dual-lock SELECT returns (id, state) tuples for both
            # source and target. Return the target as already MERGED so the
            # chain guard fires.
            if "FOR UPDATE" in str(stmt):
                return _StubResult(rows=[("tgt", "MERGED")])
            return _StubResult(scalar=self.scalar_value)

        async def scalar(self, stmt, params=None):
            return "MERGED"

    s = _ChainSession()
    import pytest

    with pytest.raises(tags_core.TagMergeChainError):
        _run(
            tags_core.merge_tag_core(
                s,
                source_tag_id="src",
                target_tag_id="tgt",
                org_id="org-1",
                actor_user_id="u-1",
            )
        )
