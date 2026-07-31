"""Decide whether the per-branch Vercel env already matches what this run
would write, so update_vercel_preview.sh can skip the env rewrite and the
force-redeploy (the auto-created deployment for the commit already baked in
the right values).

    python vercel_env_diff.py <dotenv-file> KEY=VALUE [KEY=VALUE ...]

Exits 0 when every KEY resolves to exactly VALUE in the dotenv file that
`vercel pull` wrote for the branch. Exits 1 when any value differs, the file
is missing, or a line can't be parsed -- every failure mode falls back to
the safe path (rewrite env + force redeploy), so a parse bug can only cost
a redundant rebuild, never skip a needed one.
"""

import sys

_ESCAPES = {"n": "\n", "r": "\r", "t": "\t"}


def _unescape(value):
    out = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            out.append(_ESCAPES.get(value[i + 1], value[i + 1]))
            i += 2
        else:
            out.append(char)
            i += 1
    return "".join(out)


def parse_dotenv(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
            raw = _unescape(raw[1:-1])
        values[key.strip()] = raw
    return values


def main():
    dotenv_path = sys.argv[1]
    desired = dict(pair.split("=", 1) for pair in sys.argv[2:])

    try:
        with open(dotenv_path) as f:
            current = parse_dotenv(f.read())
    except OSError as exc:
        print(f"cannot read {dotenv_path}: {exc}", file=sys.stderr)
        return 1

    changed = False
    for key, value in desired.items():
        if current.get(key) != value:
            print(f"env drift: {key} differs from the branch's current value")
            changed = True
    return 1 if changed else 0


if __name__ == "__main__":
    sys.exit(main())
