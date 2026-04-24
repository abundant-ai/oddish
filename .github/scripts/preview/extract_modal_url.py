"""Extract the Modal preview API URL from a deploy log.

Usage:
    python extract_modal_url.py <deploy_log_path>

Reads the file produced by `modal deploy ... | tee <path>` and locates the
`https://....modal.run` URL associated with the API entrypoint. Writes
`modal_api_url=<url>` to GITHUB_OUTPUT so downstream steps can consume it.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

URL_PATTERN = re.compile(r"https://[^\s]+\.modal\.run")


def _extract(text: str) -> str | None:
    candidates: list[str] = []
    for line in text.splitlines():
        if "modal.run" not in line or "api" not in line.lower():
            continue
        match = URL_PATTERN.search(line)
        if match:
            candidates.append(match.group(0))
    return candidates[-1] if candidates else None


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: extract_modal_url.py <deploy_log_path>", file=sys.stderr)
        return 2

    log_path = pathlib.Path(argv[1])
    app_name = os.environ.get("MODAL_APP_NAME", "<unknown>")
    url = _extract(log_path.read_text())
    if url is None:
        print(
            f"Could not determine Modal API URL for {app_name} from deploy output",
            file=sys.stderr,
        )
        return 1

    print(url)
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as fh:
            fh.write(f"modal_api_url={url}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
