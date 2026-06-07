#!/usr/bin/env python3
"""Drive each task's mock Slack MCP over stdio JSON-RPC and assert that every
correction token is recoverable by paginating the channel and reading threads
(a single capped search is not enough). Run from the experiment root:

    python3 mcp/smoke_test.py
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARBOR = Path("/home/user/harbor-forge-v2/tasks/skills")

CASES = [
    {
        "name": "invoice-slack-reconciliation-mcp",
        "seed": ROOT / "tasks/invoice-slack-reconciliation-mcp/environment/seed/billing_corrections_channel.json",
        "server": "billing-slack",
        "token_re": r"BILLING_FIX\{[^}]*\}",
        "source": HARBOR / "invoice-slack-reconciliation/environment/slack/billing_corrections_export.json",
    },
    {
        "name": "clinic-payables-audit-mcp",
        "seed": ROOT / "tasks/clinic-payables-audit-mcp/environment/seed/finance_ops_channel.json",
        "server": "finance-ops",
        "token_re": r"FINCORR\{[^}]*\}",
        "source": HARBOR / "clinic-payables-audit/environment/finance_ops_chat.json",
    },
]


def run_case(case) -> None:
    proc = subprocess.Popen(
        ["python3", str(ROOT / "mcp/mock_slack_mcp.py"), str(case["seed"]), case["server"]],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
    )
    counter = [0]

    def rpc(method, params=None):
        counter[0] += 1
        msg = {"jsonrpc": "2.0", "id": counter[0], "method": method}
        if params:
            msg["params"] = params
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    def call(name, args):
        return json.loads(rpc("tools/call", {"name": name, "arguments": args})["result"]["content"][0]["text"])

    assert rpc("initialize")["result"]["serverInfo"]["name"] == case["server"]
    assert {t["name"] for t in rpc("tools/list")["result"]["tools"]} == {
        "slack_list_channels", "slack_read_channel", "slack_read_thread", "slack_search_messages",
    }
    cid = call("slack_list_channels", {})["channels"][0]["id"]

    texts, threads, cursor, pages = [], [], None, 0
    while True:
        args = {"channel_id": cid}
        if cursor:
            args["cursor"] = cursor
        res = call("slack_read_channel", args)
        pages += 1
        for m in res["messages"]:
            texts.append(m.get("text", ""))
            for att in m.get("attachments", []):
                texts.extend(att.values())
            texts.extend(m.get("blocks", []))
            if m.get("reply_count", 0) > 0:
                threads.append(m["thread_ts"])
        if not res["has_more"]:
            break
        cursor = res["next_cursor"]
    for ts in threads:
        for reply in call("slack_read_thread", {"channel_id": cid, "thread_ts": ts}).get("replies", []):
            texts.append(reply["text"])

    recovered = set()
    for text in texts:
        recovered |= set(re.findall(case["token_re"], text))
    original = set(re.findall(case["token_re"], case["source"].read_text()))
    missing = original - recovered

    search = call("slack_search_messages", {"query": case["token_re"][:8].replace("\\", ""), "limit": 50})
    proc.stdin.close()
    proc.wait(timeout=5)

    assert not missing, f"{case['name']}: MISSING via MCP: {missing}"
    print(
        f"{case['name']}: {pages} pages, {len(threads)} threads, "
        f"recovered {len(recovered)}/{len(original)} tokens via pagination+threads ✓ "
        f"(search capped={search['capped']})"
    )


def main() -> int:
    for case in CASES:
        run_case(case)
    print("SMOKE OK — all tokens recoverable through the MCP for every task")
    return 0


if __name__ == "__main__":
    sys.exit(main())
