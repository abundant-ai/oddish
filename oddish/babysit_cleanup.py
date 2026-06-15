"""Classify and (optionally) soft-delete clearly-infra/no-op trials in d8d31fa9.

DRY RUN by default. Pass --delete to actually soft-delete the DELETE-classified
trials. Never touches VALID trials, ambiguous failures, or other experiments.
"""
import asyncio
import sys
import re
from sqlalchemy import text
from oddish.db import get_session, init_db
from oddish.core.endpoints import delete_trial_core

EXP = "d8d31fa9"
ORG = "8ebde5d0"

# Genuine agent timeout => VALID, never delete. Anchored to the error prefix
# ("AgentTimeout"/"... timed out") rather than scanning the whole blob, which
# embeds the task prompt.
TIMEOUT_RX = re.compile(r"timed out|agenttimeout|agent timeout", re.IGNORECASE)


def is_valid(status, reward, tok, em):
    if status == "success" and reward is not None and (tok or 0) >= 2000:
        return True
    if status == "failed" and TIMEOUT_RX.search(em or ""):
        return True
    return False


def classify(status, reward, tok, em, dur_min):
    """Return (action, label).

    In this experiment a finished trial is one of:
      - VALID (keep)
      - success below the 2000-token bar => no-op clutter (delete), unless it
        ran long with no error and just lacks token accounting (keep, review)
      - failed but not an agent-timeout => execution/infra/crash error (delete)
    """
    if is_valid(status, reward, tok, em):
        return "keep", "VALID"
    emm = em or ""
    if status == "success":
        if tok is not None and tok < 2000:
            return "delete", f"NOOP success tok={tok}"
        if tok is None:
            if emm.strip():
                return "delete", "NOOP success (killed/err, no tokens)"
            if dur_min is not None and dur_min < 15:
                return "delete", f"NOOP success (no tokens, {dur_min:.0f}m)"
            return "keep", f"AMBIGUOUS success (no tokens, {dur_min:.0f}m, no err)"
        return "delete", f"NOOP success tok={tok}"
    # failed
    if TIMEOUT_RX.search(emm):
        return "keep", "VALID (timeout)"
    return "delete", "INFRA/crash failed"


async def main():
    await init_db()
    do_delete = "--delete" in sys.argv
    q = text(
        """
        SELECT t.id, t.agent, t.task_id, t.status::text AS status, t.reward,
               t.output_tokens AS tok, t.error_message AS em,
               EXTRACT(EPOCH FROM (COALESCE(t.finished_at, NOW())
                       - COALESCE(t.started_at, t.created_at)))/60 AS dur_min
        FROM trials t
        WHERE t.experiment_id = :exp AND t.deleted_at IS NULL
          AND lower(t.status::text) IN ('success','failed')
        ORDER BY t.task_id, t.agent
        """
    )
    async with get_session() as s:
        rows = (await s.execute(q, {"exp": EXP})).mappings().all()

    to_delete = []
    keep_ambig = []
    for r in rows:
        st = (r["status"] or "").lower()
        action, label = classify(st, r["reward"], r["tok"], r["em"], r["dur_min"])
        if action == "delete":
            to_delete.append((r, label))
        elif label.startswith("AMBIG"):
            keep_ambig.append((r, label))

    print(f"Total finished trials: {len(rows)}")
    print(f"To DELETE (infra/no-op): {len(to_delete)}")
    print(f"Ambiguous failures kept for review: {len(keep_ambig)}\n")

    print("=== DELETE candidates ===")
    for r, label in to_delete:
        em = (r["em"] or "").replace("\n", " ")[:80]
        print(f"  {r['id']:42} {r['agent'][:7]:7} {r['status']:7} tok={str(r['tok']):>7} "
              f"dur={r['dur_min']:.0f}m [{label}] {em}")

    if keep_ambig:
        print("\n=== AMBIGUOUS (NOT deleting) ===")
        for r, label in keep_ambig:
            em = (r["em"] or "").replace("\n", " ")[:90]
            print(f"  {r['id']:42} {r['agent'][:7]:7} dur={r['dur_min']:.0f}m {em}")

    if do_delete:
        print(f"\nDeleting {len(to_delete)} trials...")
        ok = 0
        for r, label in to_delete:
            try:
                async with get_session() as s:
                    await delete_trial_core(s, trial_id=r["id"], org_id=ORG)
                    await s.commit()
                ok += 1
            except Exception as e:
                print(f"  !! failed to delete {r['id']}: {type(e).__name__}: {e}")
        print(f"Deleted {ok}/{len(to_delete)}.")
    else:
        print("\n(DRY RUN — pass --delete to soft-delete the DELETE candidates.)")


if __name__ == "__main__":
    asyncio.run(main())
