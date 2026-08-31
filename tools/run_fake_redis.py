from __future__ import annotations

import argparse

from fakeredis import TcpFakeServer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6379)
    args = parser.parse_args()

    server = TcpFakeServer((args.host, args.port), server_type="redis")
    print(f"Fake Redis TCP server listening on {args.host}:{args.port} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down fake Redis server.")


if __name__ == "__main__":
    main()
