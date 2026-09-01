"""``oddish delivery`` — shipping checklists over sets of tasks.

A delivery answers one question: is this set of tasks good to ship?
``oddish delivery ready`` is the scriptable gate: exit 0 when every check
is green, exit 1 with the blockers otherwise, so CI and agents can wire
it into their loops. See docs/delivery-design.md.
"""

from __future__ import annotations

from typing import Annotated, Any, Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

from oddish.cli.config import get_api_url, get_auth_headers, print_json

delivery_app = typer.Typer(
    help="Track whether a set of tasks is ready to ship to a customer.",
    no_args_is_help=True,
)

console = Console()
error_console = Console(stderr=True)

_API_OPTION = typer.Option("--api", help="API URL")
_JSON_OPTION = typer.Option("--json", help="Output JSON (for CI/scripts).")


def _fail(message: str) -> None:
    error_console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(1)


def _request(
    client: httpx.Client, method: str, url: str, **kwargs: Any
) -> Any:
    response = client.request(method, url, **kwargs)
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail") or response.text
        except Exception:  # noqa: BLE001
            detail = response.text
        _fail(f"{response.status_code} - {detail}")
    return response.json() if response.text.strip() else None


def _resolve_delivery_id(client: httpx.Client, api_url: str, ref: str) -> str:
    """Accept a delivery id or a unique delivery name."""
    deliveries = _request(client, "GET", f"{api_url}/deliveries")
    by_id = [d for d in deliveries if d["id"] == ref]
    if by_id:
        return ref
    by_name = [d for d in deliveries if d["name"] == ref]
    if len(by_name) == 1:
        return by_name[0]["id"]
    if len(by_name) > 1:
        _fail(f"delivery name {ref!r} is ambiguous; use the id")
    _fail(f"no delivery with id or name {ref!r}")
    raise AssertionError  # unreachable

def _fetch_board(client: httpx.Client, api_url: str, ref: str) -> dict:
    delivery_id = _resolve_delivery_id(client, api_url, ref)
    return _request(client, "GET", f"{api_url}/deliveries/{delivery_id}")


def _check_glyph(check: dict) -> str:
    if check["status"] == "pass":
        return f"[green]✓[/green] {check['label']}"
    if check["status"] == "off":
        return f"[dim]○ {check['label']}[/dim]"
    if check["status"] == "waived":
        return f"[yellow]![/yellow] {check['label']} (acknowledged)"
    return f"[red]✗[/red] {check['label']}"


def _blockers(board: dict) -> list[str]:
    lines = [
        f"{row['task_name']}: {check['label']}"
        + f" — {check.get('detail') or 'not done'}"
        for row in board["tasks"]
        for check in row["checks"]
        if check["status"] == "fail"
    ]
    lines += [
        f"{row['task_name']}: defect {defect['id']} — {defect['title']}"
        for row in board["tasks"]
        for defect in row.get("defects", [])
        if not defect["acknowledged"]
    ]
    lines += [
        f"delivery: {check['label']}"
        + (f" — {check['detail']}" if check.get("detail") else "")
        for check in board["delivery_checks"]
        if check["status"] == "fail"
    ]
    if not board["tasks"]:
        lines.append("delivery has no tasks")
    return lines


def _print_board(board: dict) -> None:
    delivery = board["delivery"]
    status = delivery["status"].upper()
    customer = (
        f" (customer: {delivery['customer_name']})"
        if delivery.get("customer_name")
        else ""
    )
    console.print(f"[bold]{delivery['name']}[/bold]{customer} — {status}")
    ready = (
        "[green]READY[/green]" if board["ready"] else "[yellow]NOT READY[/yellow]"
    )
    frozen = " (frozen snapshot)" if board.get("frozen") else ""
    console.print(
        f"{ready} — {board['ready_task_count']}/{board['task_count']} "
        f"tasks green{frozen}"
    )

    table = Table(show_lines=False)
    table.add_column("Task")
    table.add_column("Ver")
    table.add_column("Checks")
    table.add_column("Ready", justify="center")
    for row in board["tasks"]:
        version = f"v{row['version']}" if row.get("version") is not None else "—"
        if row.get("newer_version_exists"):
            version += "[yellow]*[/yellow]"
        table.add_row(
            row["task_name"],
            version,
            "  ".join(_check_glyph(check) for check in row["checks"]),
            "[green]✓[/green]" if row["ready"] else "[red]✗[/red]",
        )
    console.print(table)

    for check in board["delivery_checks"]:
        console.print(f"  {_check_glyph(check)}")

    blockers = _blockers(board)
    if blockers and not board["ready"]:
        console.print("[bold]Blockers:[/bold]")
        for line in blockers:
            console.print(f"  [red]-[/red] {line}")
    if any(row.get("newer_version_exists") for row in board["tasks"]):
        console.print(
            "[dim]* a newer task version exists that is not the default[/dim]"
        )


@delivery_app.command("list")
def list_deliveries(
    api_url: Annotated[str, _API_OPTION] = "",
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    """List deliveries."""
    api_url = api_url or get_api_url()
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        deliveries = _request(client, "GET", f"{api_url}/deliveries")
    if json_output:
        print_json(deliveries)
        return
    if not deliveries:
        console.print("No deliveries yet. Create one with: oddish delivery create")
        return
    table = Table()
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Customer")
    table.add_column("Status")
    table.add_column("Tasks", justify="right")
    for delivery in deliveries:
        table.add_row(
            delivery["id"],
            delivery["name"],
            delivery.get("customer_name") or "—",
            delivery["status"],
            str(delivery["task_count"]),
        )
    console.print(table)


@delivery_app.command("create")
def create_delivery(
    name: Annotated[str, typer.Argument(help="Delivery name.")],
    customer: Annotated[
        str,
        typer.Option(
            "--customer",
            help="Customer the delivery ships to (required). A new name "
            "creates the customer.",
        ),
    ],
    description: Annotated[
        Optional[str], typer.Option("--description", help="Description.")
    ] = None,
    tasks: Annotated[
        Optional[list[str]],
        typer.Option("--task", "-t", help="Task id or name to include (repeatable)."),
    ] = None,
    api_url: Annotated[str, _API_OPTION] = "",
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    """Create a delivery, optionally seeding it with tasks."""
    api_url = api_url or get_api_url()
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        delivery = _request(
            client,
            "POST",
            f"{api_url}/deliveries",
            json={
                "name": name,
                "customer": customer,
                "description": description,
                "task_ids": tasks or [],
            },
        )
    if json_output:
        print_json(delivery)
        return
    console.print(
        f"[green]Created delivery {delivery['id']}[/green] ({delivery['name']})"
    )


@delivery_app.command("show")
def show_delivery(
    delivery: Annotated[str, typer.Argument(help="Delivery id or name.")],
    api_url: Annotated[str, _API_OPTION] = "",
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    """Show the readiness board: every task, every check, every blocker."""
    api_url = api_url or get_api_url()
    with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
        board = _fetch_board(client, api_url, delivery)
    if json_output:
        print_json(board)
        return
    _print_board(board)


@delivery_app.command("ready")
def ready(
    delivery: Annotated[str, typer.Argument(help="Delivery id or name.")],
    api_url: Annotated[str, _API_OPTION] = "",
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    """The gate: exit 0 when the delivery is good to go, 1 with blockers.

    Examples:
        oddish delivery ready august-batch && ship.sh
    """
    api_url = api_url or get_api_url()
    with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
        board = _fetch_board(client, api_url, delivery)
    blockers = _blockers(board)
    if json_output:
        print_json({"ready": board["ready"], "blockers": blockers})
    elif board["ready"]:
        console.print(
            f"[green]READY[/green] — {board['task_count']} tasks, all checks green"
        )
    else:
        console.print(
            f"[yellow]NOT READY[/yellow] — "
            f"{board['ready_task_count']}/{board['task_count']} tasks green"
        )
        for line in blockers:
            console.print(f"  [red]-[/red] {line}")
    if not board["ready"]:
        raise typer.Exit(1)


@delivery_app.command("add")
def add_tasks(
    delivery: Annotated[str, typer.Argument(help="Delivery id or name.")],
    tasks: Annotated[list[str], typer.Argument(help="Task ids or names to add.")],
    api_url: Annotated[str, _API_OPTION] = "",
) -> None:
    """Add tasks to a delivery."""
    api_url = api_url or get_api_url()
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        delivery_id = _resolve_delivery_id(client, api_url, delivery)
        result = _request(
            client,
            "POST",
            f"{api_url}/deliveries/{delivery_id}/tasks",
            json={"task_ids": tasks},
        )
    console.print(f"[green]Added {result['added']} task(s)[/green]")


@delivery_app.command("remove")
def remove_task(
    delivery: Annotated[str, typer.Argument(help="Delivery id or name.")],
    task: Annotated[str, typer.Argument(help="Task id or name to remove.")],
    api_url: Annotated[str, _API_OPTION] = "",
) -> None:
    """Remove a task from a delivery."""
    api_url = api_url or get_api_url()
    with httpx.Client(timeout=30.0, headers=get_auth_headers()) as client:
        delivery_id = _resolve_delivery_id(client, api_url, delivery)
        _request(
            client, "DELETE", f"{api_url}/deliveries/{delivery_id}/tasks/{task}"
        )
    console.print(f"[green]Removed {task}[/green]")


@delivery_app.command("check")
def set_check(
    delivery: Annotated[str, typer.Argument(help="Delivery id or name.")],
    check_key: Annotated[str, typer.Argument(help="Manual check key.")],
    task: Annotated[
        Optional[str],
        typer.Option("--task", help="Task id or name (for task-scoped checks)."),
    ] = None,
    off: Annotated[
        bool, typer.Option("--off", help="Untick instead of ticking.")
    ] = False,
    note: Annotated[str, typer.Option("--note", help="Note to attach.")] = "",
    api_url: Annotated[str, _API_OPTION] = "",
) -> None:
    """Tick (or untick with --off) a manual sign-off check."""
    api_url = api_url or get_api_url()
    with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
        board = _fetch_board(client, api_url, delivery)
        delivery_task_id = None
        if task is not None:
            row = next(
                (
                    r
                    for r in board["tasks"]
                    if task in (r["task_id"], r["task_name"])
                ),
                None,
            )
            if row is None:
                _fail(f"task {task!r} is not in this delivery")
                return
            delivery_task_id = row["delivery_task_id"]
        _request(
            client,
            "PUT",
            f"{api_url}/deliveries/{board['delivery']['id']}/checks",
            json={
                "check_key": check_key,
                "delivery_task_id": delivery_task_id,
                "checked": not off,
                "note": note,
            },
        )
    verb = "Unticked" if off else "Ticked"
    console.print(f"[green]{verb} {check_key}[/green]")


def _member_row(board: dict, task: str) -> dict:
    row = next(
        (r for r in board["tasks"] if task in (r["task_id"], r["task_name"])),
        None,
    )
    if row is None:
        _fail(f"task {task!r} is not in this delivery")
        raise AssertionError  # unreachable
    return row


def _open_blockers(row: dict) -> tuple[list[dict], list[dict]]:
    """Failing automated checks that need a waive, and unacked defects."""
    checks = [
        c
        for c in row["checks"]
        if c["kind"] == "automated"
        and c["status"] == "fail"
        and c["key"] != "no_must_fix"
    ]
    defects = [d for d in row.get("defects", []) if not d["acknowledged"]]
    return checks, defects


@delivery_app.command("signoff")
def signoff(
    delivery: Annotated[str, typer.Argument(help="Delivery id or name.")],
    task: Annotated[
        str, typer.Argument(help="Task id or name. Omit with --all.")
    ] = "",
    all_clean: Annotated[
        bool,
        typer.Option(
            "--all", help="Sign off every task with no open blockers."
        ),
    ] = False,
    off: Annotated[
        bool, typer.Option("--off", help="Remove the sign-off.")
    ] = False,
    note: Annotated[str, typer.Option("--note", help="Note to attach.")] = "",
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", "-y", help="Acknowledge open blockers without a prompt."
        ),
    ] = False,
    api_url: Annotated[str, _API_OPTION] = "",
) -> None:
    """Sign a task off. The server records who signed and which version.

    If the task does not meet the requirements, the command warns, lists
    the blockers, and asks for confirmation. On yes, it records an
    acknowledgement in your name for each blocker, then signs off.

    With --all, the command signs off every task that has no open
    blockers and no sign-off yet. It lists the tasks it skipped.
    """
    if all_clean == bool(task):
        _fail("give a task or --all, not both" if task else "give a task or --all")
    if all_clean and off:
        _fail("--all cannot be combined with --off")
    api_url = api_url or get_api_url()
    with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
        board = _fetch_board(client, api_url, delivery)
        checks_url = f"{api_url}/deliveries/{board['delivery']['id']}/checks"
        if all_clean:
            signed = 0
            skipped: list[str] = []
            for row in board["tasks"]:
                signoff_check = next(
                    (c for c in row["checks"] if c["key"] == "signoff"), None
                )
                if signoff_check is None or signoff_check["status"] == "pass":
                    continue
                failing, open_defects = _open_blockers(row)
                if failing or open_defects:
                    skipped.append(row["task_name"])
                    continue
                _request(
                    client,
                    "PUT",
                    checks_url,
                    json={
                        "check_key": "signoff",
                        "delivery_task_id": row["delivery_task_id"],
                        "checked": True,
                        "note": note,
                    },
                )
                signed += 1
                console.print(f"[green]Signed off {row['task_name']}[/green]")
            if skipped:
                console.print(
                    "[yellow]Skipped (open blockers — sign off one at a "
                    "time to acknowledge):[/yellow] " + ", ".join(skipped)
                )
            if signed == 0 and not skipped:
                console.print("Nothing to sign off.")
            return
        row = _member_row(board, task)
        if not off:
            failing, open_defects = _open_blockers(row)
            if failing or open_defects:
                console.print(
                    f"[yellow]{row['task_name']} does not meet the "
                    "requirements:[/yellow]"
                )
                for check in failing:
                    detail = (
                        f" — {check['detail']}" if check.get("detail") else ""
                    )
                    console.print(f"  [red]-[/red] {check['label']}{detail}")
                for defect in open_defects:
                    console.print(
                        f"  [red]-[/red] defect {defect['id']} — "
                        f"{defect['title']}"
                    )
                if not yes and not typer.confirm(
                    "Acknowledge these in your name and sign off anyway?"
                ):
                    raise typer.Exit(1)
                for check in failing:
                    _request(
                        client,
                        "PUT",
                        checks_url,
                        json={
                            "check_key": f"waive:{check['key']}",
                            "delivery_task_id": row["delivery_task_id"],
                            "checked": True,
                        },
                    )
                for defect in open_defects:
                    _request(
                        client,
                        "PUT",
                        checks_url,
                        json={
                            "check_key": f"ack:{defect['id']}",
                            "delivery_task_id": row["delivery_task_id"],
                            "checked": True,
                        },
                    )
        _request(
            client,
            "PUT",
            checks_url,
            json={
                "check_key": "signoff",
                "delivery_task_id": row["delivery_task_id"],
                "checked": not off,
                "note": note,
            },
        )
    verb = "Removed sign-off from" if off else "Signed off"
    console.print(f"[green]{verb} {row['task_name']}[/green]")


@delivery_app.command("ack")
def ack(
    delivery: Annotated[str, typer.Argument(help="Delivery id or name.")],
    task: Annotated[str, typer.Argument(help="Task id or name.")],
    defect: Annotated[
        str,
        typer.Argument(
            help="Defect id, or the key of a failing automated check "
            "(see 'delivery show')."
        ),
    ],
    off: Annotated[
        bool, typer.Option("--off", help="Remove the acknowledgement.")
    ] = False,
    note: Annotated[str, typer.Option("--note", help="Note to attach.")] = "",
    api_url: Annotated[str, _API_OPTION] = "",
) -> None:
    """Acknowledge one must-fix defect or one failing automated check.

    The server records who did it and for which version. An acknowledged
    check no longer blocks the task."""
    api_url = api_url or get_api_url()
    with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
        board = _fetch_board(client, api_url, delivery)
        row = _member_row(board, task)
        is_check = any(
            c["key"] == defect and c["kind"] == "automated"
            for c in row["checks"]
        )
        prefix = "waive" if is_check else "ack"
        _request(
            client,
            "PUT",
            f"{api_url}/deliveries/{board['delivery']['id']}/checks",
            json={
                "check_key": f"{prefix}:{defect}",
                "delivery_task_id": row["delivery_task_id"],
                "checked": not off,
                "note": note,
            },
        )
    noun = "check" if is_check else "defect"
    verb = "Removed acknowledgement of" if off else "Acknowledged"
    console.print(f"[green]{verb} {noun} {defect}[/green]")


@delivery_app.command("finalize")
def finalize(
    delivery: Annotated[str, typer.Argument(help="Delivery id or name.")],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")
    ] = False,
    api_url: Annotated[str, _API_OPTION] = "",
) -> None:
    """Finalize a green delivery: pin task versions and freeze the record."""
    if not yes:
        typer.confirm(
            "Finalizing pins every task version and makes the delivery "
            "read-only. Continue?",
            abort=True,
        )
    api_url = api_url or get_api_url()
    with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
        delivery_id = _resolve_delivery_id(client, api_url, delivery)
        board = _request(
            client, "POST", f"{api_url}/deliveries/{delivery_id}/finalize"
        )
    console.print(
        f"[green]Finalized[/green] — {board['task_count']} tasks pinned at "
        f"{board['finalized_at']}"
    )


@delivery_app.command("history")
def history(
    task: Annotated[str, typer.Argument(help="Task id.")],
    api_url: Annotated[str, _API_OPTION] = "",
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    """Show a task's QA trail: versions, audits, rollouts, defects, QA runs."""
    api_url = api_url or get_api_url()
    with httpx.Client(timeout=60.0, headers=get_auth_headers()) as client:
        data = _request(client, "GET", f"{api_url}/tasks/{task}/qa-history")
    if json_output:
        print_json(data)
        return
    console.print(f"[bold]{data['task_name']}[/bold] ({data['task_id']})")
    verdict = data.get("verdict") or {}
    if verdict:
        label = "accept" if verdict.get("is_good") else "reject"
        color = "green" if verdict.get("is_good") else "red"
        console.print(f"Current verdict: [{color}]{label}[/{color}]")
    for version in data["versions"]:
        marker = " [cyan](current)[/cyan]" if version["is_current"] else ""
        message = f" — {version['message']}" if version.get("message") else ""
        console.print(f"\n[bold]v{version['version']}[/bold]{marker}{message}")
        audit = version.get("pre_trial_status") or "not run"
        console.print(
            f"  audit: {audit.lower()} · rollouts: {version['rollout_count']} "
            f"({version['rollout_agents']} agents) · defects: "
            f"{version['must_fix']} must-fix, "
            f"{version['pre_trial_should_fix']} should-fix"
        )
        for run in version["qa_runs"]:
            console.print(
                f"  qa run: {run['kind']} ({run.get('status') or 'pending'})"
            )
