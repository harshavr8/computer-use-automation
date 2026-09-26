"""Run the mock app:  python -m mock_app --variant base --port 5001"""
from __future__ import annotations

import argparse
import json

from .app import create_app
from .faults import FaultConfig
from .variants import VARIANTS


def main() -> None:
    p = argparse.ArgumentParser(description="Mock legacy CU member-servicing app")
    p.add_argument("--variant", default="base", choices=sorted(VARIANTS))
    p.add_argument("--port", type=int, default=5001)
    p.add_argument("--faults", default=None, help='JSON, e.g. \'{"notice_dialog": true}\'')
    p.add_argument("--no-admin", action="store_true", help="disable /__admin endpoints")
    args = p.parse_args()

    faults = FaultConfig.from_dict(json.loads(args.faults)) if args.faults else FaultConfig.from_env()
    app = create_app(variant=args.variant, faults=faults, enable_admin=not args.no_admin)
    # Loopback only: this is a local test target, never expose it.
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
