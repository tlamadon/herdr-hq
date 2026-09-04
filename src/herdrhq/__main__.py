from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys


def main() -> int:
    from .config import load_config

    ap = argparse.ArgumentParser(
        prog="herdr-hq",
        description="Fleet dashboard for herdr agents across machines.",
    )
    ap.add_argument("--config", help="path to herdr-hq.yaml (also: $HERDRHQ_CONFIG)")
    ap.add_argument("--host", default=None, help="bind address (default from config)")
    ap.add_argument("--port", type=int, default=None, help="bind port (default from config)")
    ap.add_argument("--once", action="store_true", help="poll every host once, print JSON, exit")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = load_config(args.config)
    if args.host:
        cfg.listen_host = args.host
    if args.port:
        cfg.listen_port = args.port

    if args.once:
        return asyncio.run(_once(cfg))

    import uvicorn

    from .app import create_app

    app = create_app(cfg)
    hosts = ", ".join(cfg.hosts)
    print(f"herdr-hq: watching {len(cfg.hosts)} host(s): {hosts}", flush=True)
    print(f"herdr-hq: serving http://{cfg.listen_host}:{cfg.listen_port}/", flush=True)
    uvicorn.run(app, host=cfg.listen_host, port=cfg.listen_port, log_level="warning")
    return 0


async def _once(cfg) -> int:
    from .fleet import Fleet
    from .pool import SSHPool

    pool = SSHPool()
    fleet = Fleet(cfg, pool)
    try:
        await asyncio.gather(*(p.poll_once() for p in fleet.pollers))
        json.dump(fleet.state(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
