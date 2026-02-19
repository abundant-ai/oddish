import argparse
import json

from oddish.api import run_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Oddish API server")
    parser.add_argument(
        "--n-concurrent",
        type=str,
        help="Provider concurrency as JSON (e.g., '{\"claude\": 8}')",
    )
    parser.add_argument("--host", type=str, help="API host")
    parser.add_argument("--port", type=int, help="API port")

    args = parser.parse_args()

    concurrency = None
    if args.n_concurrent:
        concurrency = json.loads(args.n_concurrent)

    run_server(concurrency=concurrency, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
