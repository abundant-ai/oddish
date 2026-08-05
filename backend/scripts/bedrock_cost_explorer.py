"""Pull actual Amazon Bedrock spend from AWS Cost Explorer.

``trials.cost_usd`` already carries Bedrock spend, but at *list price*: the
claude-code CLI computes ``total_cost_usd`` from a client-side Anthropic price
table and ``settle_cost_usd`` trusts it (see ``oddish.model_pricing``). That
number never sees an AWS invoice, so provisioned throughput, committed-use
discounts, cross-region inference surcharges, and private pricing are all
invisible to it. This script fetches what AWS actually billed so
``bedrock_cost_attribution.py`` can reconcile the two.

Cost Explorer only answers in the payer/management account and needs
``ce:GetCostAndUsage`` -- a grant separate from the ``bedrock:InvokeModel``
permission the worker runtime uses. Point it at management-account credentials,
not the runtime key.

    cd backend
    # 1. Find which service names carry Bedrock spend in your account.
    uv run python scripts/bedrock_cost_explorer.py discover \
        --start 2026-03-01 --end 2026-08-05

    # 2. Pull monthly spend split by usage type.
    uv run python scripts/bedrock_cost_explorer.py fetch \
        --start 2026-03-01 --end 2026-08-05 --out bedrock_ce.json

Read-only: issues Cost Explorer queries and writes a local JSON file. CE bills
about $0.01 per request.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

# Bedrock model spend does not always bill under a service literally named
# "Amazon Bedrock" -- third-party models can land under marketplace-style names.
# `discover` reports everything matching, so a real account can correct this.
SERVICE_PATTERN = re.compile(r"bedrock|anthropic|claude", re.IGNORECASE)

# Cost Explorer settles with a lag; the last day or two of a window can move.
UNBLENDED = "UnblendedCost"


def _client(region: str, profile: str | None):
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    # Cost Explorer is a global service with a us-east-1 endpoint.
    return session.client("ce", region_name=region)


def _paginate(client, **kwargs) -> list[dict[str, Any]]:
    """GetCostAndUsage with NextPageToken follow-through."""
    pages: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        if token:
            kwargs["NextPageToken"] = token
        response = client.get_cost_and_usage(**kwargs)
        pages.extend(response.get("ResultsByTime", []))
        token = response.get("NextPageToken")
        if not token:
            return pages


def _amount(metrics: dict[str, Any], key: str = UNBLENDED) -> float:
    return float(metrics.get(key, {}).get("Amount", 0.0) or 0.0)


def discover(args: argparse.Namespace) -> int:
    """List every service with spend, flagging the Bedrock-looking ones.

    Run this first: the usage-type strings it prints are what
    ``bedrock_cost_attribution.py`` maps onto ``trials.model``.
    """
    client = _client(args.region, args.profile)
    results = _paginate(
        client,
        TimePeriod={"Start": args.start, "End": args.end},
        Granularity="MONTHLY",
        Metrics=[UNBLENDED],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    totals: dict[str, float] = {}
    for period in results:
        for group in period.get("Groups", []):
            totals[group["Keys"][0]] = totals.get(group["Keys"][0], 0.0) + _amount(
                group["Metrics"]
            )

    matched = {s: v for s, v in totals.items() if SERVICE_PATTERN.search(s)}
    print(f"=== SERVICES WITH SPEND {args.start}..{args.end} ===\n")
    for service, amount in sorted(totals.items(), key=lambda kv: -kv[1])[:40]:
        flag = "  <- BEDROCK?" if service in matched else ""
        print(f"  ${amount:>14,.2f}  {service}{flag}")

    if not matched:
        print("\nNo Bedrock-looking service found. Widen SERVICE_PATTERN or")
        print("pass --service explicitly to `fetch`.")
        return 1

    print(f"\n=== USAGE TYPES for {', '.join(sorted(matched))} ===\n")
    usage = _paginate(
        client,
        TimePeriod={"Start": args.start, "End": args.end},
        Granularity="MONTHLY",
        Metrics=[UNBLENDED],
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": sorted(matched)}},
    )
    ut_totals: dict[str, float] = {}
    for period in usage:
        for group in period.get("Groups", []):
            ut_totals[group["Keys"][0]] = ut_totals.get(
                group["Keys"][0], 0.0
            ) + _amount(group["Metrics"])
    for ut, amount in sorted(ut_totals.items(), key=lambda kv: -kv[1]):
        print(f"  ${amount:>14,.2f}  {ut}")
    print(f"\n{len(ut_totals)} usage types. Feed these to bedrock_cost_attribution.py")
    print("via `--write-mapping` to build the usage-type -> trials.model mapping.")
    return 0


def fetch(args: argparse.Namespace) -> int:
    """Write per-period, per-usage-type Bedrock spend to JSON."""
    client = _client(args.region, args.profile)

    group_by: list[dict[str, str]] = [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}]
    if args.tag_key:
        # Forward-looking: once application inference profiles carry cost
        # allocation tags, this is the axis that attributes without pro-rata.
        group_by = [{"Type": "TAG", "Key": args.tag_key}] + group_by

    services = args.service or None
    cost_filter: dict[str, Any] | None = None
    if services:
        cost_filter = {"Dimensions": {"Key": "SERVICE", "Values": services}}

    kwargs: dict[str, Any] = {
        "TimePeriod": {"Start": args.start, "End": args.end},
        "Granularity": args.granularity,
        "Metrics": [UNBLENDED, "UsageQuantity"],
        "GroupBy": group_by,
    }
    if cost_filter:
        kwargs["Filter"] = cost_filter

    results = _paginate(client, **kwargs)

    records: list[dict[str, Any]] = []
    for period in results:
        start = period["TimePeriod"]["Start"]
        for group in period.get("Groups", []):
            keys = group["Keys"]
            metrics = group["Metrics"]
            records.append(
                {
                    "period_start": start,
                    "period_end": period["TimePeriod"]["End"],
                    "tag": keys[0] if args.tag_key else None,
                    "usage_type": keys[-1],
                    "amount_usd": _amount(metrics),
                    "usage_quantity": float(
                        metrics.get("UsageQuantity", {}).get("Amount", 0.0) or 0.0
                    ),
                    "unit": metrics.get("UsageQuantity", {}).get("Unit"),
                }
            )

    with open(args.out, "w") as fh:
        json.dump(records, fh, indent=2)

    total = sum(r["amount_usd"] for r in records)
    print(f"wrote {args.out}: {len(records)} records, ${total:,.2f} total")
    by_period: dict[str, float] = {}
    for r in records:
        by_period[r["period_start"]] = (
            by_period.get(r["period_start"], 0.0) + r["amount_usd"]
        )
    for period in sorted(by_period):
        print(f"  {period}  ${by_period[period]:>12,.2f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--profile", help="AWS profile (management account)")
    parser.add_argument("--region", default="us-east-1", help="CE endpoint region")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="list services and usage types")
    d.add_argument("--start", required=True, help="inclusive, YYYY-MM-DD")
    d.add_argument("--end", required=True, help="exclusive, YYYY-MM-DD")
    d.set_defaults(func=discover)

    f = sub.add_parser("fetch", help="write spend to JSON")
    f.add_argument("--start", required=True, help="inclusive, YYYY-MM-DD")
    f.add_argument("--end", required=True, help="exclusive, YYYY-MM-DD")
    f.add_argument("--out", default="bedrock_ce.json")
    f.add_argument("--granularity", default="MONTHLY", choices=["MONTHLY", "DAILY"])
    f.add_argument(
        "--service",
        action="append",
        help="SERVICE dimension value; repeatable. Omit to query all services.",
    )
    f.add_argument("--tag-key", help="also group by this cost allocation tag")
    f.set_defaults(func=fetch)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except NoCredentialsError:
        print(
            "No AWS credentials. Cost Explorer needs management-account creds with\n"
            "ce:GetCostAndUsage -- the worker runtime's bedrock:InvokeModel key will\n"
            "not work. Pass --profile or set AWS_PROFILE.",
            file=sys.stderr,
        )
        return 2
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"AccessDeniedException", "UnauthorizedException"}:
            print(
                f"Access denied ({code}). Cost Explorer requires ce:GetCostAndUsage\n"
                "and only answers in the payer/management account.",
                file=sys.stderr,
            )
            return 3
        if code == "DataUnavailableException":
            print(
                "Cost Explorer has no data for that window. It is not enabled\n"
                "retroactively -- data starts when CE was first turned on.",
                file=sys.stderr,
            )
            return 4
        raise


if __name__ == "__main__":
    raise SystemExit(main())
