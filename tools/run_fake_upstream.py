from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_upstream import FakeUpstreamServer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000, help="Must match GATEWAY_UPSTREAM_BASE_URL's port.")
    parser.add_argument(
        "--content",
        default="On file: card 4111 1111 1111 1111, annual salary $185,000.",
        help="The assistant message content the fake upstream will always reply with.",
    )
    args = parser.parse_args()

    server = FakeUpstreamServer(host=args.host, port=args.port)
    server.set_response_content(args.content)
    server.start()

    print(f"Fake upstream AI agent listening on http://{args.host}:{args.port}")
    print(f"Every request will get back: {args.content!r}")
    print("Point GATEWAY_UPSTREAM_BASE_URL at this address. Ctrl+C to stop.")

    try:
        server._thread.join()
    except KeyboardInterrupt:
        print("\nShutting down fake upstream server.")
        server.stop()


if __name__ == "__main__":
    main()
