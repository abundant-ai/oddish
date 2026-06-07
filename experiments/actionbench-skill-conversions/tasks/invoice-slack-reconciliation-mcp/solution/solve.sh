#!/bin/bash
set -e

python3 <<'PY'
import csv
import os
import json
import re
from collections import defaultdict
from datetime import datetime
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import openpyxl

WORKBOOK_PATH = os.environ.get("INVOICE_WORKBOOK", "/root/invoice_register.xlsx")
SLACK_PATH = os.environ.get("BILLING_SEED", "/opt/slack-mcp/seed/billing_corrections_channel.json")
CLOSE_DATE = date(2026, 5, 31)

FIELDNAMES = [
    "row_type",
    "invoice_id",
    "original_invoice_id",
    "invoice_date",
    "due_date",
    "payment_terms_days",
    "customer_id",
    "customer_name",
    "account_owner",
    "description",
    "status",
    "currency",
    "fx_rate_to_usd",
    "amount",
    "paid_amount",
    "balance",
    "amount_usd",
    "paid_amount_usd",
    "balance_usd",
    "days_past_due",
    "aging_bucket",
    "reserved_amount_usd",
    "source_sheet",
]

ALLOWED_FIELDS = {"status", "paid_amount", "amount", "account_owner", "description", "add_payment"}
STATUS_MAP = {
    "open": "Open",
    "paid": "Paid",
    "disputed": "Disputed",
    "void": "Void",
}
TOKEN_RE = re.compile(r"BILLING_FIX\{([^}]+)\}", re.IGNORECASE)


def money(value):
    if value is None or str(value).strip() == "":
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = str(value).strip().replace("$", "").replace(",", "")
    return Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money_text(value):
    return f"{money(value):.2f}"


def money_float(value):
    return float(money(value))


def normalize_status(value):
    key = str(value or "").strip().lower()
    if key not in STATUS_MAP:
        return ""
    return STATUS_MAP[key]


def normalize_date(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return datetime.fromisoformat(str(value).strip()).strftime("%Y-%m-%d")


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    return datetime.fromisoformat(str(value).strip()).date()


def is_record_id(value):
    text = str(value or "").strip()
    return bool(re.fullmatch(r"INV-\d{4}", text) or re.fullmatch(r"CM-\d{4}", text))


def find_header(rows):
    expected = {"row_type", "invoice_id", "invoice_date", "customer_id", "customer_name", "status", "amount", "paid_amount"}
    for idx, row in enumerate(rows):
        names = {str(cell or "").strip().lower() for cell in row}
        if expected.issubset(names):
            return idx, {str(cell or "").strip().lower(): pos for pos, cell in enumerate(row)}
    return None, None


def read_reference_sheets(wb):
    fx_rates = {}
    if "FX_Rates" in wb.sheetnames:
        ws = wb["FX_Rates"]
        rows = list(ws.iter_rows(values_only=True))
        headers = {str(v or "").strip().lower(): i for i, v in enumerate(rows[0])}
        for row in rows[1:]:
            currency = str(row[headers["currency"]] or "").strip().upper()
            if currency:
                fx_rates[currency] = money(row[headers["usd_rate"]])
    aliases = {}
    if "Customer_Aliases" in wb.sheetnames:
        ws = wb["Customer_Aliases"]
        rows = list(ws.iter_rows(values_only=True))
        headers = {str(v or "").strip().lower(): i for i, v in enumerate(rows[0])}
        for row in rows[1:]:
            alias = str(row[headers["alias_name"]] or "").strip()
            if alias:
                aliases[alias.casefold()] = {
                    "customer_id": str(row[headers["canonical_customer_id"]] or "").strip(),
                    "customer_name": str(row[headers["canonical_customer_name"]] or "").strip(),
                }
    if not fx_rates:
        fx_rates = {"USD": Decimal("1.00")}
    return fx_rates, aliases


def read_workbook():
    wb = openpyxl.load_workbook(WORKBOOK_PATH, data_only=True)
    fx_rates, aliases = read_reference_sheets(wb)
    records = {}
    source_sheets = [name for name in wb.sheetnames if name not in {"FX_Rates", "Customer_Aliases"}]
    for sheet_index, sheet_name in enumerate(source_sheets):
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        header_idx, cols = find_header(rows)
        if header_idx is None:
            continue
        current_owner = ""
        for row_index, row in enumerate(rows[header_idx + 1 :], start=header_idx + 1):
            owner_cell = row[cols["account_owner"]] if cols["account_owner"] < len(row) else None
            if owner_cell and str(owner_cell).strip() and str(owner_cell).strip().lower() not in {"section subtotal", "grand total"}:
                current_owner = str(owner_cell).strip()
            record_id = row[cols["invoice_id"]] if cols["invoice_id"] < len(row) else None
            record_id = str(record_id or "").strip()
            if not is_record_id(record_id):
                continue
            row_type = str(row[cols["row_type"]] or "INVOICE").strip().upper()
            if row_type not in {"INVOICE", "CREDIT"}:
                continue
            customer_name = str(row[cols["customer_name"]] or "").strip()
            alias = aliases.get(customer_name.casefold())
            if alias:
                customer_id = alias["customer_id"]
                customer_name = alias["customer_name"]
            else:
                customer_id = str(row[cols["customer_id"]] or "").strip()
            currency = str(row[cols["currency"]] or "").strip().upper()
            fx_rate = fx_rates[currency]
            amount = money(row[cols["amount"]])
            paid_amount = money(row[cols["paid_amount"]])
            if row_type == "CREDIT":
                amount = -abs(amount)
                paid_amount = Decimal("0.00")
            record = {
                "row_type": row_type,
                "invoice_id": record_id,
                "original_invoice_id": str(row[cols["original_invoice_id"]] or "").strip(),
                "invoice_date": normalize_date(row[cols["invoice_date"]]),
                "due_date": normalize_date(row[cols["due_date"]]),
                "payment_terms_days": int(row[cols["payment_terms_days"]] or 0),
                "customer_id": customer_id,
                "customer_name": customer_name,
                "account_owner": current_owner,
                "description": str(row[cols["description"]] or "").strip(),
                "status": normalize_status(row[cols["status"]]),
                "currency": currency,
                "fx_rate_to_usd": fx_rate,
                "amount": amount,
                "paid_amount": paid_amount,
                "source_sheet": sheet_name,
                "_sheet_index": sheet_index,
                "_row_index": row_index,
            }
            records[record_id] = record
    return records


def collect_text(obj, ts, out):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "ts" and isinstance(value, str):
                ts = value
            elif key in {"text", "fallback", "pretext"}:
                if isinstance(value, str):
                    out.append((ts, value))
                else:
                    collect_text(value, ts, out)
            elif isinstance(value, (dict, list)):
                collect_text(value, ts, out)
    elif isinstance(obj, list):
        for item in obj:
            collect_text(item, ts, out)


def parse_token(body, ts):
    parts = {}
    for segment in body.split(","):
        if ":" not in segment:
            continue
        key, value = segment.split(":", 1)
        parts[key.strip().lower()] = value.strip()
    field = parts.get("field", "").strip().lower()
    invoice_id = parts.get("invoice", "").strip()
    if not invoice_id or field not in ALLOWED_FIELDS or "to" not in parts:
        return None
    try:
        priority = int(parts.get("priority", "0"))
    except ValueError:
        priority = 0
    return {
        "slack_ts": ts,
        "invoice_id": invoice_id,
        "field": field,
        "to": parts["to"].strip(),
        "priority": priority,
    }


def read_corrections():
    with open(SLACK_PATH, encoding="utf-8") as f:
        export = json.load(f)
    blobs = []
    collect_text(export.get("messages", []), "", blobs)
    corrections = []
    for ts, text in blobs:
        for match in TOKEN_RE.finditer(text or ""):
            parsed = parse_token(match.group(1), ts)
            if parsed:
                corrections.append(parsed)
    return corrections


def apply_status_rules(record):
    if record["row_type"] == "CREDIT":
        record["paid_amount"] = Decimal("0.00")
    elif record["status"] == "Void":
        record["amount"] = Decimal("0.00")
        record["paid_amount"] = Decimal("0.00")
    elif record["status"] == "Paid":
        record["paid_amount"] = record["amount"]
    record["balance"] = money(record["amount"] - record["paid_amount"])
    fx = record["fx_rate_to_usd"]
    record["amount_usd"] = money(record["amount"] * fx)
    record["paid_amount_usd"] = money(record["paid_amount"] * fx)
    record["balance_usd"] = money(record["balance"] * fx)
    balance_usd = record["balance_usd"]
    if balance_usd <= Decimal("0.00"):
        record["days_past_due"] = 0
        record["aging_bucket"] = "settled"
        record["reserved_amount_usd"] = Decimal("0.00")
        return
    days = max(0, (CLOSE_DATE - parse_date(record["due_date"])).days)
    record["days_past_due"] = days
    if days == 0:
        bucket = "current"
    elif days <= 30:
        bucket = "1-30"
    elif days <= 60:
        bucket = "31-60"
    else:
        bucket = "61+"
    record["aging_bucket"] = bucket
    if record["status"] == "Disputed":
        rate = {"current": Decimal("0.25"), "1-30": Decimal("0.25"), "31-60": Decimal("0.50"), "61+": Decimal("0.75")}[bucket]
    elif record["status"] in {"Paid", "Void"}:
        rate = Decimal("0.00")
    else:
        rate = {"current": Decimal("0.00"), "1-30": Decimal("0.02"), "31-60": Decimal("0.05"), "61+": Decimal("0.20")}[bucket]
    record["reserved_amount_usd"] = money(balance_usd * rate)


def display_value(field, record):
    if field in {"amount", "paid_amount", "balance", "add_payment"}:
        return money_text(record["paid_amount"] if field == "add_payment" else record[field])
    return str(record[field])


records = read_workbook()
corrections = read_corrections()

skipped_unknown = []
by_key = defaultdict(list)
for correction in corrections:
    if correction["invoice_id"] not in records:
        skipped_unknown.append(
            {
                "slack_ts": correction["slack_ts"],
                "invoice_id": correction["invoice_id"],
                "field": correction["field"],
                "reason": "invoice not in workbook",
            }
        )
        continue
    by_key[(correction["invoice_id"], correction["field"])].append(correction)

winning_patches = []
superseded = []
winners = []
for key, candidates in by_key.items():
    winner = max(candidates, key=lambda item: (item["priority"], item["slack_ts"]))
    winners.append(winner)
    for candidate in candidates:
        if candidate is winner:
            continue
        superseded.append(
            {
                "slack_ts": candidate["slack_ts"],
                "invoice_id": candidate["invoice_id"],
                "field": candidate["field"],
                "reason": "lower priority or older timestamp on tie",
            }
        )

for winner in sorted(winners, key=lambda item: item["slack_ts"]):
    record = records[winner["invoice_id"]]
    field = winner["field"]
    previous = display_value(field, record)
    if field == "status":
        record[field] = normalize_status(winner["to"])
    elif field in {"amount", "paid_amount"}:
        record[field] = money(winner["to"])
    elif field == "add_payment":
        record["paid_amount"] = money(record["paid_amount"] + money(winner["to"]))
    else:
        record[field] = winner["to"]
    winning_patches.append(
        {
            "slack_ts": winner["slack_ts"],
            "invoice_id": winner["invoice_id"],
            "field": field,
            "previous_value": previous,
            "new_value": display_value(field, record),
            "priority": winner["priority"],
        }
    )

for record in records.values():
    apply_status_rules(record)

ledger_rows = []
for record in records.values():
    row = {}
    for field in FIELDNAMES:
        if field in {"amount", "paid_amount", "balance", "amount_usd", "paid_amount_usd", "balance_usd", "reserved_amount_usd", "fx_rate_to_usd"}:
            continue
        row[field] = record[field]
    for field in ("amount", "paid_amount", "balance", "amount_usd", "paid_amount_usd", "balance_usd", "reserved_amount_usd"):
        row[field] = money_text(record[field])
    row["fx_rate_to_usd"] = money_text(record["fx_rate_to_usd"])
    ledger_rows.append(row)

ledger_rows.sort(key=lambda row: (row["invoice_date"], row["invoice_id"]))

with open("/root/invoice_ledger.csv", "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
    writer.writeheader()
    writer.writerows(ledger_rows)

customer_groups = defaultdict(list)
for row in ledger_rows:
    customer_groups[row["customer_id"]].append(row)

summary_money_keys = [
    "current_balance_usd",
    "days_1_30_balance_usd",
    "days_31_60_balance_usd",
    "days_61_plus_balance_usd",
    "total_amount_usd",
    "total_paid_usd",
    "total_balance_usd",
    "total_reserved_usd",
]
count_keys = ["invoice_count", "credit_count", "open_count", "paid_count", "disputed_count", "void_count"]
totals = {key: 0 for key in count_keys}
totals.update({key: Decimal("0.00") for key in summary_money_keys})
customers = []
for customer_id in sorted(customer_groups):
    rows = customer_groups[customer_id]
    invoice_rows = [row for row in rows if row["row_type"] == "INVOICE"]
    summary = {
        "customer_id": customer_id,
        "customer_name": rows[0]["customer_name"],
        "invoice_count": len(invoice_rows),
        "credit_count": sum(1 for row in rows if row["row_type"] == "CREDIT"),
        "open_count": sum(1 for row in invoice_rows if row["status"] == "Open"),
        "paid_count": sum(1 for row in invoice_rows if row["status"] == "Paid"),
        "disputed_count": sum(1 for row in invoice_rows if row["status"] == "Disputed"),
        "void_count": sum(1 for row in invoice_rows if row["status"] == "Void"),
        "current_balance_usd": sum((money(row["balance_usd"]) for row in rows if row["aging_bucket"] == "current"), Decimal("0.00")),
        "days_1_30_balance_usd": sum((money(row["balance_usd"]) for row in rows if row["aging_bucket"] == "1-30"), Decimal("0.00")),
        "days_31_60_balance_usd": sum((money(row["balance_usd"]) for row in rows if row["aging_bucket"] == "31-60"), Decimal("0.00")),
        "days_61_plus_balance_usd": sum((money(row["balance_usd"]) for row in rows if row["aging_bucket"] == "61+"), Decimal("0.00")),
        "total_amount_usd": sum((money(row["amount_usd"]) for row in rows), Decimal("0.00")),
        "total_paid_usd": sum((money(row["paid_amount_usd"]) for row in rows), Decimal("0.00")),
        "total_balance_usd": sum((money(row["balance_usd"]) for row in rows), Decimal("0.00")),
        "total_reserved_usd": sum((money(row["reserved_amount_usd"]) for row in rows), Decimal("0.00")),
    }
    for key in count_keys:
        totals[key] += summary[key]
    for key in summary_money_keys:
        totals[key] += summary[key]
        summary[key] = money_float(summary[key])
    customers.append(summary)

for key in summary_money_keys:
    totals[key] = money_float(totals[key])

with open("/root/customer_summary.json", "w", encoding="utf-8") as f:
    json.dump({"customers": customers, "totals": totals}, f, indent=2)

winning_patches.sort(key=lambda item: item["slack_ts"])
with open("/root/correction_audit.json", "w", encoding="utf-8") as f:
    json.dump(
        {
            "winning_patches": winning_patches,
            "superseded": sorted(superseded, key=lambda item: item["slack_ts"]),
            "skipped_unknown_invoice": sorted(skipped_unknown, key=lambda item: item["slack_ts"]),
        },
        f,
        indent=2,
    )

print("Done: invoice_ledger.csv, customer_summary.json, correction_audit.json")
PY
