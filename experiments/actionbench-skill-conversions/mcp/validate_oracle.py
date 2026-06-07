#!/usr/bin/env python3
"""Validate each converted task: run its oracle (which reads the channel SEED
directly) and then run the REUSED verifier against the oracle's outputs. This
proves the conversion preserves the expected outputs (seed = original tokens +
token-free noise) and that the reused pytest verifier still passes.

Mirrors the in-container layout by staging file inputs under /root, then cleans
up. Run from the experiment root (needs openpyxl/pandas/pytest):

    python3 mcp/validate_oracle.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARBOR = Path("/home/user/harbor-forge-v2/tasks/skills")
ROOT_HOME = Path("/root")

CASES = [
    {
        "name": "invoice-slack-reconciliation-mcp",
        "inputs": {HARBOR / "invoice-slack-reconciliation/environment/invoice_register.xlsx": "invoice_register.xlsx"},
        "seed_env": ("BILLING_SEED", ROOT / "tasks/invoice-slack-reconciliation-mcp/environment/seed/billing_corrections_channel.json"),
        "outputs": ["invoice_ledger.csv", "customer_summary.json", "correction_audit.json"],
    },
    {
        "name": "clinic-payables-audit-mcp",
        "inputs": {
            HARBOR / "clinic-payables-audit/environment/invoice_ledger.csv": "invoice_ledger.csv",
            HARBOR / "clinic-payables-audit/environment/bank_statement.csv": "bank_statement.csv",
            HARBOR / "clinic-payables-audit/environment/vendor_master.csv": "vendor_master.csv",
            HARBOR / "clinic-payables-audit/environment/bank_change_tickets.csv": "bank_change_tickets.csv",
        },
        "seed_env": ("FINANCE_CHAT_SEED", ROOT / "tasks/clinic-payables-audit-mcp/environment/seed/finance_ops_channel.json"),
        "outputs": ["reconciled_payments.csv", "reconciliation_summary.json", "adjustment_timeline.json"],
    },
]


def run_case(case) -> bool:
    task_dir = ROOT / "tasks" / case["name"]
    staged = []
    for src, dest in case["inputs"].items():
        shutil.copy(src, ROOT_HOME / dest)
        staged.append(ROOT_HOME / dest)
    env = dict(os.environ)
    key, value = case["seed_env"]
    env[key] = str(value)
    subprocess.run(["bash", str(task_dir / "solution/solve.sh")], check=True, env=env)
    result = subprocess.run(
        ["python3", "-m", "pytest", "-q", str(task_dir / "tests/test_outputs.py")],
        capture_output=True, text=True,
    )
    print(result.stdout.strip().splitlines()[-1] if result.stdout else "(no output)")
    # cleanup staged inputs + produced outputs
    for path in staged + [ROOT_HOME / o for o in case["outputs"]]:
        path.unlink(missing_ok=True)
    ok = result.returncode == 0
    print(f"{case['name']}: {'PASS' if ok else 'FAIL'} (oracle -> reused verifier)")
    return ok


def main() -> int:
    ok = all(run_case(case) for case in CASES)
    print("VALIDATE OK" if ok else "VALIDATE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
