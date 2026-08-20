#!/usr/bin/env python3
"""Delete the credential-failure trials in an experiment, keep the real ones.

The opencode/google trials that predate the GOOGLE_GENERATIVE_AI_API_KEY mirror
never reached the model: `Command failed (exit 1)` on the opencode launch line,
zero tokens, no trajectory. They carry no signal and must not be scored.

Trials that ran for real (hundreds of thousands of input tokens) are KEPT --
including the post-fix smoke trials.

A trial still in flight is never deleted: it may be a good one mid-run.

Usage: clear_broken.py <experiment_id> [--apply]
"""
import json, subprocess, sys, os, collections

ODDISH = "/home/user/oddish/oddish/.venv/bin/oddish"
PENDING = {"pending", "queued", "running", "blocked", "preparing", "submitted",
           "claimed", "in_progress", "initializing", "retrying"}
TASKS = {
    "biofabric-rust-rewrite": "biofabric-rust-rewrite-897c0fc8",
    "embedding-eval": "embedding-eval-be1dc228",
    "excel-clone": "excel-clone-685bff89",
    "find-network-alignments": "find-network-alignments-b2ecebb7",
    "jax-pytorch-rewrite": "jax-pytorch-rewrite-93c38683",
    "kubernetes-rust-rewrite": "kubernetes-rust-rewrite-bae0f616",
    "mastodon-clone": "mastodon-clone-e9964b7a",
    "nextjs-vite-rewrite": "nextjs-vite-rewrite-b0d028b0",
    "parameter-golf": "parameter-golf-61e11605",
    "post-train-ifeval-gpu": "post-train-ifeval-gpu-1de1166a",
    "ruby-rust-port": "ruby-rust-port-383776ff",
    "rust-c-compiler": "rust-c-compiler-57913cbe",
    "rust-java-lsp": "rust-java-lsp-0a9c67d6",
    "s3-clone": "s3-clone-23996371",
    "slack-clone": "slack-clone-b0a98beb",
    "stripe-clone": "stripe-clone-66061f4d",
    "trimul-cuda": "trimul-cuda-fc34aaba",
    "vliw-kernel-optimization": "vliw-kernel-optimization-e4d25121",
    "wasm-simd": "wasm-simd-40e18127",
    "zstd-decoder": "zstd-decoder-72bbfded",
}


def load_env():
    env = dict(os.environ)
    for line in open("/home/user/oddish/.env"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def api(args, env, tries=8):
    import time
    for i in range(tries):
        try:
            r = subprocess.run([ODDISH] + args, capture_output=True, text=True,
                               env=env, timeout=600)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
        except Exception:
            pass
        time.sleep(min(5 * (i + 1), 30))
    return None


def main():
    exp = sys.argv[1]
    apply = "--apply" in sys.argv
    env = load_env()
    broken, kept, inflight, unread = [], 0, 0, []

    for name, tid in TASKS.items():
        d = api(["status", tid, "--json"], env)
        if d is None:
            unread.append(name)
            continue
        for tr in d.get("trials") or []:
            if tr.get("experiment_id") != exp:
                continue
            if tr.get("superseded_by_trial_id") is not None:
                continue
            st = (tr.get("status") or "").lower()
            if st in PENDING:
                inflight += 1
                continue
            err = (tr.get("error_message") or "").lower()
            tok = tr.get("input_tokens") or 0
            # Never reached the model: the launch failed, or it burned no tokens
            # at all. Either way there is nothing to score.
            if "command failed" in err or tok == 0:
                broken.append((name, tr.get("id"), tok, (tr.get("error_message") or "")[:50]))
            else:
                kept += 1

    if unread:
        sys.exit(f"ABORT: {len(unread)} tasks unreadable ({unread[:5]}). "
                 "Refusing to delete on partial data.")

    print(f"broken (delete): {len(broken)}   real (keep): {kept}   in flight (skip): {inflight}")
    for name, tid, tok, err in broken[:8]:
        print(f"   {name:26} {tid:34} tok={tok} {err}")
    if len(broken) > 8:
        print(f"   ... and {len(broken)-8} more")

    if not apply:
        print("(dry run — pass --apply to delete)")
        return
    if not broken:
        return
    denv = dict(env)
    denv["ODDISH_API_KEY"] = denv["ODDISH_ADMIN_API_KEY"]
    ids = [t[1] for t in broken]
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        args = [ODDISH, "delete"] + sum([["-t", x] for x in chunk], []) + ["--json"]
        r = subprocess.run(args, capture_output=True, text=True, env=denv, timeout=600)
        print(f"deleted {len(chunk)}: rc={r.returncode}")


if __name__ == "__main__":
    main()
