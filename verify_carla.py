#!/usr/bin/env python3
"""Verify that a CARLA Python client can query a running simulator."""

from __future__ import annotations

import argparse
import json
import sys

import carla


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=2000, type=int)
    parser.add_argument("--timeout", default=30.0, type=float)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = carla.Client(args.host, args.port)
    client.set_timeout(args.timeout)

    world = client.get_world()
    snapshot = world.get_snapshot()
    result = {
        "actor_count": len(world.get_actors()),
        "client_version": client.get_client_version(),
        "frame": snapshot.frame,
        "map": world.get_map().name,
        "server_version": client.get_server_version(),
    }

    print("CARLA Python API verification: PASS")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"CARLA Python API verification: FAIL: {exc}", file=sys.stderr)
        raise
