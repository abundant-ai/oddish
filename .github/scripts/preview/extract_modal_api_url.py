"""Pull the deployed Modal API URL out of a captured `modal deploy`
log. The main API's webhook label ends in `api`; auxiliary endpoints such
as the QA-model gateway end in `api-qa-model` and must not be selected.

    python extract_modal_api_url.py <log_path>

Prints the unique API URL on stdout; exits non-zero if it is missing or
ambiguous so the workflow cannot route the frontend to another endpoint.
"""

import pathlib
import re
import sys


def main():
    log_path = pathlib.Path(sys.argv[1])
    text = log_path.read_text()

    candidates = set(re.findall(r"https://[a-zA-Z0-9-]+-api\.modal\.run\b", text))
    if len(candidates) != 1:
        raise SystemExit(
            f"Expected one Modal API URL in {log_path}; found {sorted(candidates)}"
        )
    print(candidates.pop())


if __name__ == "__main__":
    main()
