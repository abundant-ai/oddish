#!/bin/bash
set -e

python3 <<'PY'
import os
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal

INVOICE_RE = re.compile(r"\bINV-\d+\b", re.IGNORECASE)
FINCORR_RE = re.compile(r"FINCORR\{([^}]+)\}", re.IGNORECASE)
EXCEPTION_ORDER = [
    "unmatched_payment",
    "amount_mismatch",
    "duplicate_invoice_payment",
    "held_by_finance_ops",
    "high_risk_late_payment",
    "account_last4_mismatch",
]


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


def money_str(value):
    return f"{money(value):.2f}"


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def collect_text(node, ts="", out=None):
    if out is None:
        out = []
    if isinstance(node, dict):
        current_ts = node.get("ts", ts)
        for key in ("text", "fallback", "pretext"):
            val = node.get(key)
            if isinstance(val, str) and val:
                out.append((current_ts, val))
        for key in ("messages", "attachments", "blocks", "thread_replies"):
            for child in node.get(key) or []:
                collect_text(child, current_ts, out)
    elif isinstance(node, list):
        for child in node:
            collect_text(child, ts, out)
    return out


def parse_token(body, chat_ts):
    parts = {}
    for segment in body.split(","):
        if ":" not in segment:
            continue
        key, value = segment.split(":", 1)
        parts[key.strip().lower()] = value.strip()
    adj_type = parts.get("type", "").lower()
    if adj_type not in {"remap", "hold", "release"}:
        return None
    try:
        priority = int(parts.get("priority", "0"))
    except ValueError:
        return None
    item = {
        "chat_ts": chat_ts,
        "type": adj_type,
        "priority": priority,
    }
    if "payment" in parts:
        item["payment_id"] = parts["payment"].upper()
    if "invoice" in parts:
        item["invoice_id"] = parts["invoice"].upper()
    if "reason" in parts:
        item["reason"] = parts["reason"]
    if adj_type == "remap" and ("payment_id" not in item or "invoice_id" not in item):
        return None
    if adj_type in {"hold", "release"} and "invoice_id" not in item:
        return None
    return item


invoices = {row["invoice_id"].upper(): row for row in read_csv("/root/invoice_ledger.csv")}
vendors = {row["vendor_id"]: row for row in read_csv("/root/vendor_master.csv")}
change_tickets = read_csv("/root/bank_change_tickets.csv")
bank_rows = read_csv("/root/bank_statement.csv")

payments = []
for row in bank_rows:
    amt = money(row["amount"])
    if amt >= 0:
        continue
    memo_invoice_ids = sorted({m.group(0).upper() for m in INVOICE_RE.finditer(row["memo"] or "")})
    payments.append(
        {
            "payment_id": row["payment_id"].upper(),
            "payment_date": row["posted_date"],
            "amount": -amt,
            "currency": row["currency"],
            "memo_invoice_ids": memo_invoice_ids,
            "account_last4": row["account_last4"],
        }
    )

payment_ids = {p["payment_id"] for p in payments}
with open(os.environ.get("FINANCE_CHAT_SEED", "/opt/slack-mcp/seed/finance_ops_channel.json"), encoding="utf-8") as f:
    chat = json.load(f)

adjustments = []
for ts, text in collect_text(chat):
    for match in FINCORR_RE.finditer(text):
        parsed = parse_token(match.group(1), ts)
        if parsed:
            adjustments.append(parsed)

skipped = []
remap_groups = defaultdict(list)
decision_groups = defaultdict(list)
for adj in adjustments:
    if adj["type"] == "remap":
        if adj.get("payment_id") not in payment_ids or adj.get("invoice_id") not in invoices:
            bad = adj.get("payment_id") if adj.get("payment_id") not in payment_ids else adj.get("invoice_id")
            skipped.append(
                {
                    "chat_ts": adj["chat_ts"],
                    "type": adj["type"],
                    "unknown_reference": bad,
                    "reason": "unknown payment or invoice",
                }
            )
            continue
        remap_groups[adj["payment_id"]].append(adj)
    else:
        if adj.get("invoice_id") not in invoices:
            skipped.append(
                {
                    "chat_ts": adj["chat_ts"],
                    "type": adj["type"],
                    "unknown_reference": adj.get("invoice_id"),
                    "reason": "unknown payment or invoice",
                }
            )
            continue
        decision_groups[adj["invoice_id"]].append(adj)

winning_adjustments = []
superseded = []
winning_remaps = {}
invoice_decisions = {}

for payment_id, group in remap_groups.items():
    winner = max(group, key=lambda item: (item["priority"], item["chat_ts"]))
    winning_remaps[payment_id] = winner
    winning_adjustments.append(winner)
    for loser in group:
        if loser is not winner:
            superseded.append(
                {
                    "chat_ts": loser["chat_ts"],
                    "type": loser["type"],
                    "payment_id": loser["payment_id"],
                    "invoice_id": loser["invoice_id"],
                    "reason": "lower priority or older timestamp on tie",
                }
            )

for invoice_id, group in decision_groups.items():
    winner = max(group, key=lambda item: (item["priority"], item["chat_ts"]))
    invoice_decisions[invoice_id] = winner
    winning_adjustments.append(winner)
    for loser in group:
        if loser is not winner:
            superseded.append(
                {
                    "chat_ts": loser["chat_ts"],
                    "type": loser["type"],
                    "invoice_id": loser["invoice_id"],
                    "reason": "lower priority or older timestamp on tie",
                }
            )

for item in winning_adjustments:
    if "payment_id" not in item:
        item.pop("payment_id", None)
winning_adjustments.sort(key=lambda item: item["chat_ts"])

resolved_counts = Counter()
for payment in payments:
    remap = winning_remaps.get(payment["payment_id"])
    payment["remapped"] = bool(remap)
    payment["invoice_ids"] = [remap["invoice_id"]] if remap else payment["memo_invoice_ids"]
    payment["invoice_ids"] = [invoice_id for invoice_id in payment["invoice_ids"] if invoice_id in invoices]
    for invoice_id in payment["invoice_ids"]:
        resolved_counts[invoice_id] += 1


def approved_account_override(vendor_id, account_last4, payment_date):
    pay_date = parse_date(payment_date)
    for ticket in change_tickets:
        if ticket["vendor_id"] != vendor_id:
            continue
        if ticket["new_account_last4"] != account_last4:
            continue
        if ticket["status"].lower() != "approved":
            continue
        if parse_date(ticket["effective_date"]) <= pay_date:
            return True
    return False


def discounted_invoice_amount(invoice, vendor, payment_date):
    base = money(invoice["amount"])
    discount_bps = int(vendor.get("discount_bps") or 0)
    if discount_bps <= 0:
        return base
    days_early = (parse_date(invoice["due_date"]) - parse_date(payment_date)).days
    if days_early < 5:
        return base
    discount = (base * Decimal(discount_bps) / Decimal("10000")).quantize(Decimal("0.01"))
    return base - discount

rows = []
summary = {
    "totals": {
        "outgoing_payments": len(payments),
        "clean_payments": 0,
        "exception_payments": 0,
        "matched_amount": "0.00",
        "exception_amount": "0.00",
    },
    "by_risk_tier": {},
    "exceptions_by_code": {},
}
by_risk = defaultdict(lambda: {"payments": 0, "amount": Decimal("0.00"), "exception_payments": 0})
exception_counts = Counter()
matched_amount = Decimal("0.00")
exception_amount = Decimal("0.00")

for payment in payments:
    invoice_ids = payment["invoice_ids"]
    resolved_invoices = [invoices[invoice_id] for invoice_id in invoice_ids]
    exceptions = []
    vendor = None
    days_late = ""
    if not resolved_invoices:
        exceptions.append("unmatched_payment")
        status = "unmatched"
        vendor_id = vendor_name = risk_tier = ""
    else:
        matched_amount += payment["amount"]
        vendor_id = resolved_invoices[0]["vendor_id"]
        vendor = vendors[vendor_id]
        vendor_name = vendor["vendor_name"]
        risk_tier = vendor["risk_tier"]
        days_late_int = max(
            (parse_date(payment["payment_date"]) - parse_date(invoice["due_date"])).days
            for invoice in resolved_invoices
        )
        days_late = str(days_late_int)
        expected_amount = sum(
            (discounted_invoice_amount(invoice, vendors[invoice["vendor_id"]], payment["payment_date"]) for invoice in resolved_invoices),
            Decimal("0.00"),
        )
        if payment["amount"] != money(expected_amount):
            exceptions.append("amount_mismatch")
        if any(resolved_counts[invoice_id] > 1 for invoice_id in invoice_ids):
            exceptions.append("duplicate_invoice_payment")
        decision = next((invoice_decisions[invoice_id] for invoice_id in invoice_ids if invoice_id in invoice_decisions), None)
        if decision and decision["type"] == "hold":
            exceptions.append("held_by_finance_ops")
        if vendor["risk_tier"].lower() == "high" and days_late_int > 0:
            exceptions.append("high_risk_late_payment")
        if payment["account_last4"] != vendor["approved_account_last4"] and not approved_account_override(
            vendor_id, payment["account_last4"], payment["payment_date"]
        ):
            exceptions.append("account_last4_mismatch")

        if decision:
            if decision["type"] == "hold":
                status = "held"
            elif any(resolved_counts[invoice_id] > 1 for invoice_id in invoice_ids):
                status = "duplicate_invoice_payment"
            elif payment["amount"] != money(expected_amount):
                status = "amount_mismatch"
            elif payment["remapped"]:
                status = "remapped"
            else:
                status = "matched"
        elif any(resolved_counts[invoice_id] > 1 for invoice_id in invoice_ids):
            status = "duplicate_invoice_payment"
        elif payment["amount"] != money(expected_amount):
            status = "amount_mismatch"
        elif payment["remapped"]:
            status = "remapped"
        else:
            status = "matched"

    ordered_exceptions = [code for code in EXCEPTION_ORDER if code in exceptions]
    for code in ordered_exceptions:
        exception_counts[code] += 1

    if ordered_exceptions:
        summary["totals"]["exception_payments"] += 1
        exception_amount += payment["amount"]
    else:
        summary["totals"]["clean_payments"] += 1

    if resolved_invoices:
        tier_stats = by_risk[risk_tier]
        tier_stats["payments"] += 1
        tier_stats["amount"] += payment["amount"]
        if ordered_exceptions:
            tier_stats["exception_payments"] += 1

    rows.append(
        {
            "payment_id": payment["payment_id"],
            "payment_date": payment["payment_date"],
            "invoice_id": ";".join(invoice_ids),
            "vendor_id": vendor_id,
            "vendor_name": vendor_name,
            "amount": money_str(payment["amount"]),
            "currency": payment["currency"],
            "match_status": status,
            "days_late": days_late,
            "risk_tier": risk_tier,
            "exception_code": "|".join(ordered_exceptions),
        }
    )

rows.sort(key=lambda row: (row["payment_date"], row["payment_id"]))
with open("/root/reconciled_payments.csv", "w", newline="", encoding="utf-8") as f:
    fieldnames = [
        "payment_id",
        "payment_date",
        "invoice_id",
        "vendor_id",
        "vendor_name",
        "amount",
        "currency",
        "match_status",
        "days_late",
        "risk_tier",
        "exception_code",
    ]
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

summary["totals"]["matched_amount"] = money_str(matched_amount)
summary["totals"]["exception_amount"] = money_str(exception_amount)
summary["by_risk_tier"] = {
    tier: {
        "payments": stats["payments"],
        "amount": money_str(stats["amount"]),
        "exception_payments": stats["exception_payments"],
    }
    for tier, stats in sorted(by_risk.items())
}
summary["exceptions_by_code"] = {code: exception_counts[code] for code in EXCEPTION_ORDER if exception_counts[code]}

with open("/root/reconciliation_summary.json", "w", encoding="utf-8") as f:
    json.dump(summary, f, indent=2)

timeline = {
    "winning_adjustments": winning_adjustments,
    "superseded": sorted(superseded, key=lambda item: item["chat_ts"]),
    "skipped_unknown_reference": sorted(skipped, key=lambda item: item["chat_ts"]),
}
with open("/root/adjustment_timeline.json", "w", encoding="utf-8") as f:
    json.dump(timeline, f, indent=2)

print("Wrote reconciled_payments.csv, reconciliation_summary.json, adjustment_timeline.json")
PY
