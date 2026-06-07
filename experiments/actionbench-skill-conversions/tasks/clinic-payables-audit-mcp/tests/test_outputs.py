import csv
import json


CSV_PATH = "/root/reconciled_payments.csv"
SUMMARY_PATH = "/root/reconciliation_summary.json"
TIMELINE_PATH = "/root/adjustment_timeline.json"

EXPECTED_COLUMNS = [
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


def _rows():
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        assert reader.fieldnames == EXPECTED_COLUMNS, (
            f"Expected CSV columns {EXPECTED_COLUMNS}, got {reader.fieldnames}"
        )
        return rows


def _by_payment():
    return {row["payment_id"]: row for row in _rows()}


def test_reconciled_payments_shape_and_sorting():
    rows = _rows()
    assert len(rows) == 12, f"Expected 12 outgoing payments, got {len(rows)}"
    assert "DEP-1042" not in {row["payment_id"] for row in rows}, "Positive deposit must be ignored"
    sort_keys = [(row["payment_date"], row["payment_id"]) for row in rows]
    assert sort_keys == sorted(sort_keys), "Rows must be sorted by payment_date then payment_id"
    assert len(sort_keys) == len(set(sort_keys)), "Payment rows must not be duplicated"


def test_reconciliation_classifications_and_edge_cases():
    rows = _by_payment()

    assert rows["PAY-8804"]["invoice_id"] == "INV-5004", "PAY-8804 should be remapped from chat attachment"
    assert rows["PAY-8804"]["match_status"] == "remapped", f"Unexpected PAY-8804 status: {rows['PAY-8804']}"

    assert rows["PAY-8805"]["invoice_id"] == "INV-5006", "PAY-8805 should use the higher-priority remap"
    assert rows["PAY-8805"]["days_late"] == "1", f"PAY-8805 days_late should be 1, got {rows['PAY-8805']['days_late']}"

    assert rows["PAY-8803"]["match_status"] == "matched", f"PAY-8803 should stay matched, got {rows['PAY-8803']}"
    assert rows["PAY-8803"]["exception_code"] == "high_risk_late_payment", (
        f"PAY-8803 should be a late high-risk payment, got {rows['PAY-8803']['exception_code']}"
    )

    assert rows["PAY-8806"]["match_status"] == "held", f"PAY-8806 should be held, got {rows['PAY-8806']}"
    assert rows["PAY-8806"]["exception_code"] == (
        "held_by_finance_ops|high_risk_late_payment|account_last4_mismatch"
    ), f"PAY-8806 exception codes are wrong: {rows['PAY-8806']['exception_code']}"

    for payment_id in ("PAY-8807", "PAY-8808"):
        assert rows[payment_id]["match_status"] == "duplicate_invoice_payment", (
            f"{payment_id} should be a duplicate invoice payment, got {rows[payment_id]}"
        )
        assert rows[payment_id]["exception_code"] == "duplicate_invoice_payment", (
            f"{payment_id} duplicate exception missing: {rows[payment_id]['exception_code']}"
        )

    assert rows["PAY-8809"]["match_status"] == "amount_mismatch", f"PAY-8809 should mismatch amount: {rows['PAY-8809']}"
    assert rows["PAY-8809"]["amount"] == "4900.00", f"PAY-8809 amount should be positive string, got {rows['PAY-8809']['amount']}"

    assert rows["PAY-8810"]["match_status"] == "matched", f"PAY-8810 should be matched despite account change: {rows['PAY-8810']}"
    assert rows["PAY-8810"]["exception_code"] == "high_risk_late_payment", (
        f"PAY-8810 should only carry high-risk late exception, got {rows['PAY-8810']['exception_code']}"
    )

    assert rows["PAY-8811"]["invoice_id"] == "INV-5010;INV-5011", (
        f"PAY-8811 should resolve a batch invoice list, got {rows['PAY-8811']['invoice_id']}"
    )
    assert rows["PAY-8811"]["days_late"] == "1", f"PAY-8811 should use max days_late 1, got {rows['PAY-8811']['days_late']}"
    assert rows["PAY-8811"]["exception_code"] == "", f"PAY-8811 should be clean, got {rows['PAY-8811']}"

    assert rows["PAY-8812"]["match_status"] == "matched", f"PAY-8812 early discount should be matched: {rows['PAY-8812']}"
    assert rows["PAY-8812"]["amount"] == "990.00", f"PAY-8812 amount should remain paid amount, got {rows['PAY-8812']['amount']}"
    assert rows["PAY-8812"]["exception_code"] == "", f"PAY-8812 discount should prevent mismatch, got {rows['PAY-8812']}"


def test_reconciliation_summary_totals_and_risk_rollups():
    with open(SUMMARY_PATH, encoding="utf-8") as f:
        summary = json.load(f)

    assert summary["totals"] == {
        "outgoing_payments": 12,
        "clean_payments": 6,
        "exception_payments": 6,
        "matched_amount": "84200.95",
        "exception_amount": "53750.00",
    }, f"Unexpected totals: {summary['totals']}"

    assert summary["by_risk_tier"]["low"] == {
        "payments": 5,
        "amount": "23140.40",
        "exception_payments": 0,
    }, f"Unexpected low-risk rollup: {summary['by_risk_tier'].get('low')}"
    assert summary["by_risk_tier"]["medium"] == {
        "payments": 3,
        "amount": "13510.55",
        "exception_payments": 2,
    }, f"Unexpected medium-risk rollup: {summary['by_risk_tier'].get('medium')}"
    assert summary["by_risk_tier"]["high"] == {
        "payments": 4,
        "amount": "47550.00",
        "exception_payments": 4,
    }, f"Unexpected high-risk rollup: {summary['by_risk_tier'].get('high')}"

    assert summary["exceptions_by_code"] == {
        "amount_mismatch": 1,
        "duplicate_invoice_payment": 2,
        "held_by_finance_ops": 1,
        "high_risk_late_payment": 3,
        "account_last4_mismatch": 1,
    }, f"Unexpected exception counts: {summary['exceptions_by_code']}"


def test_adjustment_timeline():
    with open(TIMELINE_PATH, encoding="utf-8") as f:
        timeline = json.load(f)

    assert set(timeline) == {
        "winning_adjustments",
        "superseded",
        "skipped_unknown_reference",
    }, f"Unexpected timeline keys: {timeline.keys()}"

    winners = timeline["winning_adjustments"]
    assert [item["chat_ts"] for item in winners] == sorted(item["chat_ts"] for item in winners), (
        "Winning adjustments must be sorted by chat_ts"
    )
    assert len(winners) == 3, f"Expected 3 winning adjustments, got {len(winners)}"
    assert any(item.get("payment_id") == "PAY-8805" and item["priority"] == 5 for item in winners), (
        f"Missing high-priority PAY-8805 remap winner: {winners}"
    )
    assert any(item.get("payment_id") == "PAY-8804" and item["invoice_id"] == "INV-5004" for item in winners), (
        f"Missing PAY-8804 attachment remap winner: {winners}"
    )
    assert any(item["type"] == "hold" and item["invoice_id"] == "INV-5005" for item in winners), (
        f"Missing INV-5005 hold winner: {winners}"
    )

    superseded = timeline["superseded"]
    assert len(superseded) == 2, f"Expected 2 superseded adjustments, got {superseded}"
    assert any(item.get("payment_id") == "PAY-8805" for item in superseded), (
        f"Expected lower-priority PAY-8805 remap to be superseded: {superseded}"
    )
    assert any(item["type"] == "release" and item["invoice_id"] == "INV-5005" for item in superseded), (
        f"Expected release for INV-5005 to be superseded: {superseded}"
    )

    skipped = timeline["skipped_unknown_reference"]
    assert skipped == [
        {
            "chat_ts": "2026-04-23T07:45:00Z",
            "type": "hold",
            "unknown_reference": "INV-9999",
            "reason": "unknown payment or invoice",
        }
    ], f"Unexpected skipped adjustments: {skipped}"
