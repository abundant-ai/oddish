from __future__ import annotations


class Colors:
    """ANSI color codes for verbose classifier output."""

    BLUE = "\033[94m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def print_process_stream(label: str, content: str, color: str) -> None:
    """Print subprocess output with light formatting for debug logs."""
    text = content.strip()
    if not text:
        return
    print(f"\n{color}[{label}]{Colors.RESET}\n{text}", flush=True)
