"""Pull the deployed Modal API URL out of a captured `modal deploy`
log. Modal prints one URL per registered function; we want the
public-facing API endpoint, which is the last line that mentions both
`modal.run` and `api`.

    python extract_modal_api_url.py <log_path>

Prints the URL on stdout; exits non-zero if no candidate is found so
the workflow fails visibly instead of carrying an empty URL forward.
"""

import pathlib
import re
import sys


def main():
    log_path = pathlib.Path(sys.argv[1])
    text = log_path.read_text()

    candidates = []
    for line in text.splitlines():
        if "modal.run" not in line:
            continue
        if "api" not in line.lower():
            continue
        match = re.search(r"https://[^\s]+\.modal\.run", line)
        if match:
            candidates.append(match.group(0))

    if not candidates:
        raise SystemExit(
            f"Could not determine Modal API URL from {log_path}"
        )
    print(candidates[-1])


if __name__ == "__main__":
    main()
