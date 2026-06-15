"""Tag permission planes.

The USE plane (view + apply) is open to any org member with write access
to the target. The DEFINITION plane (rename / merge / delete / edit /
manage-grants) is locked to the tag's owner, an org admin, or principals
explicitly granted via ``tag_grants``.

Admin bypass is exclusive to Clerk owner / admin roles — API-key auth
falls back to scope checks, so a TASKS-scope key can apply existing tags
but cannot e.g. RENAME a tag.

OSS-standalone (no Clerk): governance is documented as degraded; anyone
with TASKS+ scope may modify any tag in their org. The helpers below
return ``True`` whenever ``auth.user_role`` is unset AND the Clerk-backed
``OrganizationModel`` is unavailable.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text


def _role_value(auth: Any) -> str | None:
    role = (auth.user and auth.user.role) or auth.user_role
    if role is None:
        return None
    if isinstance(role, str):
        return role
    return getattr(role, "value", None)


def is_org_admin(auth: Any) -> bool:
    """True if the actor is an org admin or owner via Clerk JWT."""
    if getattr(auth, "method", None) == "api_key":
        return False
    return _role_value(auth) in {"admin", "owner"}


def _is_oss_standalone(auth: Any) -> bool:
    """Best-effort detection that Clerk is not in play.

    In cloud, ``auth.user`` carries a UserModel with a role. In OSS the
    Clerk webhook never fires, so ``user_role`` is None and ``user`` is
    None too. We then degrade to "every authenticated principal acts as
    admin".
    """
    return _role_value(auth) is None and auth.user is None


_WRITE_SCOPES = {"full", "tasks"}


def can_use_apply(
    auth: Any,
    *,
    target_org_id: str | None,
    reserved: bool = False,
) -> bool:
    """USE-plane: can this principal apply/unapply a tag on a target?

    Requires:
      * scope ∈ {full, tasks} (TASKS or higher),
      * matching org (cross-org tag use forbidden),
      * if the tag's key is in a reserved namespace, also require admin.
    """
    if getattr(auth, "scope", None) not in _WRITE_SCOPES:
        return False
    if target_org_id is not None and auth.org_id != target_org_id:
        return False
    if reserved and not is_org_admin(auth):
        return False
    return True


async def can_definition_capability(
    session: Any,
    auth: Any,
    *,
    tag: dict,
    capability: str,
) -> bool:
    """DEFINITION-plane: can the actor perform RENAME/MERGE/DELETE/EDIT?

    Order of checks:
      1. Org admin / owner bypass.
      2. OSS-standalone degraded mode (no Clerk role attached).
      3. The actor is the tag's ``owner_user_id``.
      4. A USER grant in ``tag_grants`` matches the actor + capability.
      5. An ALL_MEMBERS grant in ``tag_grants`` matches the capability.
    """
    if is_org_admin(auth):
        return True
    if _is_oss_standalone(auth):
        # OSS users with write scope skip ownership checks.
        return getattr(auth, "scope", None) in _WRITE_SCOPES
    actor_id = (auth.user and auth.user.id) or auth.user_id
    if actor_id and tag.get("owner_user_id") == actor_id:
        return True
    row = await session.scalar(
        text(
            """
            SELECT 1 FROM tag_grants
            WHERE deleted_at IS NULL
              AND tag_id = :tag_id
              AND capability = :capability
              AND (
                  (principal_type = 'USER' AND principal_user_id = :actor_id)
               OR (principal_type = 'ALL_MEMBERS')
              )
            LIMIT 1
            """
        ),
        {"tag_id": tag["id"], "capability": capability, "actor_id": actor_id},
    )
    return row is not None


def can_manage_grants(auth: Any, *, tag: dict) -> bool:
    """MANAGE_GRANTS is NOT delegatable — owner or admin only.

    Anti-escalation rule. A granted EDIT capability does not
    grant MANAGE_GRANTS.
    """
    if is_org_admin(auth):
        return True
    actor_id = (auth.user and auth.user.id) or auth.user_id
    if actor_id and tag.get("owner_user_id") == actor_id:
        return True
    return False
