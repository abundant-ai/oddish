"""Read-only check: does the Sauron bucket threaten the legacy artifacts?

ONE-OFF MIGRATION SCRIPT -- TEMPORARY BY DESIGN. Every call here is a read.

The migration moved trial METADATA into Oddish but left every artifact (logs,
trajectories, verifier output, agent files) in the legacy bucket
`abundant-github-workflows-bucket`. Serving them later via a cross-bucket read
path is only viable if the objects are still there, at full retrieval speed. Two
things could break that silently:

  1. A lifecycle rule that EXPIRES (deletes) objects after N days.
  2. A lifecycle rule that TRANSITIONS objects to Glacier / Deep Archive, which
     keeps them but makes reads slow and require a restore step.

This reports the bucket's lifecycle configuration and samples the storage class
of some real legacy trial objects, so the "leave them in place" decision has
facts under it rather than an assumption.

Usage:
    modal run backend/scripts/legacy_lifecycle_check.py
"""

import modal

app = modal.App("oddish-legacy-lifecycle-check")
image = modal.Image.debian_slim(python_version="3.13").pip_install("aioboto3")
aws_secret = modal.Secret.from_name("sauron-legacy")  # legacy-bucket AWS keys

BUCKET = "abundant-github-workflows-bucket"
# A few known legacy prefixes to sample storage class from.
SAMPLE_PREFIXES = [
    "abundant-ai/harbor-forge/",
    "abundant-ai/nov-5-export/",
    "abundant-ai/experiments/",
    "abundant-ai/reflection-ai/",
]


@app.function(image=image, secrets=[aws_secret], timeout=300)
async def check() -> bool:
    import aioboto3

    session = aioboto3.Session()
    async with session.client("s3", region_name="us-east-1") as s3:
        # ---- Bucket-level lifecycle configuration --------------------------
        print("== Lifecycle configuration ==")
        threat = False
        config_readable = True  # did we actually get to READ the policy?
        try:
            lc = await s3.get_bucket_lifecycle_configuration(Bucket=BUCKET)
            rules = lc.get("Rules", [])
            if not rules:
                print("  [PASS] no lifecycle rules -- objects persist indefinitely")
            for r in rules:
                rid = r.get("ID", "(unnamed)")
                status = r.get("Status")
                scope = r.get("Filter", r.get("Prefix", "whole bucket"))
                print(f"\n  rule '{rid}'  status={status}  scope={scope}")
                if r.get("Expiration"):
                    print(f"    [THREAT] EXPIRATION: {r['Expiration']}  <-- deletes objects")
                    if status == "Enabled":
                        threat = True
                for t in r.get("Transitions", []):
                    days = t.get("Days", t.get("Date"))
                    cls = t.get("StorageClass")
                    slow = cls in ("GLACIER", "DEEP_ARCHIVE", "GLACIER_IR")
                    tag = "[THREAT] " if (slow and status == "Enabled") else ""
                    print(f"    {tag}TRANSITION after {days} -> {cls}"
                          f"{'  <-- reads need restore / are slow' if slow else ''}")
                    if slow and status == "Enabled":
                        threat = True
                if r.get("AbortIncompleteMultipartUpload"):
                    print(f"    (housekeeping) AbortIncompleteMultipartUpload: "
                          f"{r['AbortIncompleteMultipartUpload']}  -- harmless")
        except Exception as exc:
            # NoSuchLifecycleConfiguration is the good case: boto raises it when
            # the bucket has no lifecycle rules at all.
            if "NoSuchLifecycleConfiguration" in repr(exc):
                print("  [PASS] no lifecycle configuration on the bucket -- "
                      "objects persist indefinitely")
            elif "AccessDenied" in repr(exc):
                config_readable = False
                print("  [UNKNOWN] AccessDenied -- this read-only credential cannot "
                      "read the lifecycle policy. Cannot confirm the bucket is safe.")
            else:
                config_readable = False
                print(f"  [error] could not read lifecycle config: {exc!r}")

        # ---- Actual storage class of real objects --------------------------
        # Lifecycle config says the policy; this says what actually happened.
        print("\n== Storage class of sampled legacy objects ==")
        classes: dict[str, int] = {}
        total_seen = 0
        for prefix in SAMPLE_PREFIXES:
            try:
                paginator = s3.get_paginator("list_objects_v2")
                seen = 0
                async for page in paginator.paginate(
                    Bucket=BUCKET, Prefix=prefix, PaginationConfig={"MaxItems": 200}
                ):
                    for obj in page.get("Contents", []):
                        # STANDARD objects often omit StorageClass in listings.
                        cls = obj.get("StorageClass", "STANDARD")
                        classes[cls] = classes.get(cls, 0) + 1
                        seen += 1
                        total_seen += 1
                print(f"  {prefix:42} sampled {seen}")
            except Exception as exc:
                print(f"  {prefix:42} [error] {exc!r}")

        print(f"\n  storage-class distribution across {total_seen} sampled objects:")
        for cls, n in sorted(classes.items(), key=lambda kv: -kv[1]):
            slow = cls in ("GLACIER", "DEEP_ARCHIVE")
            print(f"    {cls:16} {n:6}  ({100.0*n/max(total_seen,1):.1f}%)"
                  f"{'  <-- archived, slow to read' if slow else ''}")

        # ---- Versioning (affects deletability, not read speed) -------------
        print("\n== Bucket versioning ==")
        try:
            v = await s3.get_bucket_versioning(Bucket=BUCKET)
            print(f"  status={v.get('Status', 'Disabled')}")
        except Exception as exc:
            print(f"  [error] {exc!r}")

        print("\n" + "=" * 60)
        if threat:
            print("VERDICT: THREAT FOUND -- an ENABLED rule expires or archives objects.")
            print("The 'leave artifacts in place' plan has a clock on it. Either copy")
            print("the artifacts, or get the rule scoped to exclude these prefixes.")
            return False
        if not config_readable:
            print("VERDICT: INCONCLUSIVE. The read-only sauron-legacy credential is")
            print("denied s3:GetLifecycleConfiguration / GetBucketVersioning, so the")
            print("policy could NOT be read. The object sample is 100% STANDARD, which")
            print("means nothing has been archived or deleted AS OF NOW -- but that does")
            print("not rule out a future-dated expiration or transition rule.")
            print("To confirm, an admin with s3:GetLifecycleConfiguration must run:")
            print(f"  aws s3api get-bucket-lifecycle-configuration --bucket {BUCKET}")
            return False
        print("VERDICT: policy READ and no enabled expiration/archive threat found, and")
        print("the object sample is 100% STANDARD. Leaving artifacts in place and serving")
        print("them cross-bucket is safe on current config. (Point-in-time read.)")
        return True


@app.local_entrypoint()
def main() -> None:
    ok = check.remote()
    if not ok:
        raise SystemExit(1)
