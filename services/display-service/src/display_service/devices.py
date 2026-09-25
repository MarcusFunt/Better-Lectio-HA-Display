"""Manage display credentials locally, printing a generated secret once."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .device_registry import DeviceRegistry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.getenv("DISPLAY_DATA_DIR", "/var/lib/better-lectio-display")),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="register a device and generate its secret")
    create.add_argument("name")
    create.add_argument("--device-id")
    rotate = commands.add_parser("rotate", help="rotate an active device credential")
    rotate.add_argument("device_id")
    revoke = commands.add_parser("revoke", help="revoke a device")
    revoke.add_argument("device_id")
    commands.add_parser("list", help="list device metadata without credentials")
    args = parser.parse_args()
    registry = DeviceRegistry(args.data_dir)

    try:
        if args.command == "create":
            created = registry.create(args.name, device_id=args.device_id)
            print(
                json.dumps(
                    {"device_id": created.record.device_id, "device_secret": created.secret},
                    separators=(",", ":"),
                )
            )
        elif args.command == "rotate":
            rotated = registry.rotate(args.device_id)
            print(
                json.dumps(
                    {"device_id": rotated.record.device_id, "device_secret": rotated.secret},
                    separators=(",", ":"),
                )
            )
        elif args.command == "revoke":
            record = registry.revoke(args.device_id)
            print(json.dumps({"device_id": record.device_id, "revoked": True}))
        else:
            print(
                json.dumps(
                    [
                        {
                            "device_id": record.device_id,
                            "name": record.name,
                            "created_at": record.created_at,
                            "last_seen": record.last_seen,
                            "revoked": record.revoked,
                            "firmware_version": record.firmware_version,
                            "last_content_hash": record.last_content_hash,
                        }
                        for record in registry.list_devices()
                    ]
                )
            )
    except (KeyError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
