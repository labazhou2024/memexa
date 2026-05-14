"""PG-aware batch_id cache — single source of truth for "already in graph".

Solves the marker-drift problem common to incremental backfill drivers:
- ghost markers (.posted written but PG never stored card) → driver thinks
  batch is done, real backlog stays missed
- PG-no-marker rows (worker died before writing the sidecar) → driver
  re-extracts every cron run, the daemon dedupe rejects it, GPU wasted

By querying PG directly with a 1-hour TTL cache, drivers get authoritative
ground truth. Cache cost: 1 psql query per source per hour (≤500 ms each).

## Two transport modes

Default = direct ``psycopg2`` connection to PG. Use this when the PG
process listens on a port your machine can reach (localhost, LAN, or
a tunneled port).

Optional = ``ssh <host> psql ...`` when the PG port is firewalled and
you can only reach it via a remote shell. Set
``MEMEXA_PG_SSH_TARGET`` to enable.

Usage in drivers:

    from src.core.pg_bid_cache import query_pg_existing_bids
    pg_bids = query_pg_existing_bids("wechat")  # cached set[str]
    pending = [b for b in input_bids if b not in pg_bids and b not in markers]

Cache file (JSON):

    data/pg_bid_cache/<source>.json = {
        "fetched_ts": 1715000000.0,
        "ttl_sec": 3600,
        "bids": ["abc123...", ...]
    }

Env overrides:

    MEMEXA_PG_DSN          full DSN (overrides host/port/user/db)
    MEMEXA_PG_HOST         default: 127.0.0.1
    MEMEXA_PG_PORT         default: 5433
    MEMEXA_PG_USER         default: $USER (i.e. whoami)
    MEMEXA_PG_DB           default: hindsight
    MEMEXA_HINDSIGHT_BANK  default: memory_full_v5
    MEMEXA_PG_BID_TTL_SEC  default: 3600

    # SSH-mode (optional, only needed when PG port is firewalled):
    MEMEXA_PG_SSH_TARGET   default: '' (disabled — use direct psycopg2)
    MEMEXA_PG_PSQL_BIN     default: 'psql' (must be on PATH on the remote)

CLI smoke-test:

    python -m src.core.pg_bid_cache wechat
    python -m src.core.pg_bid_cache cc --force-refresh
    python -m src.core.pg_bid_cache all
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_CACHE_DIR = _REPO / "data" / "pg_bid_cache"

# ssh-mode (opt-in: leave empty to use psycopg2 directly)
_SSH_TARGET = os.environ.get("MEMEXA_PG_SSH_TARGET", "").strip()
_PSQL_BIN = os.environ.get("MEMEXA_PG_PSQL_BIN", "psql")

_PG_DSN = os.environ.get("MEMEXA_PG_DSN", "").strip()
_PG_DB = os.environ.get("MEMEXA_PG_DB", "hindsight")
_PG_HOST = os.environ.get("MEMEXA_PG_HOST", "127.0.0.1")
_PG_PORT = os.environ.get("MEMEXA_PG_PORT", "5433")
_PG_USER = os.environ.get("MEMEXA_PG_USER", "") or getpass.getuser()
_PG_BANK = os.environ.get("MEMEXA_HINDSIGHT_BANK", "memory_full_v5")
_DEFAULT_TTL = int(os.environ.get("MEMEXA_PG_BID_TTL_SEC", "3600"))

# Driver source → PG metadata source value
_DRIVER_TO_PG_SOURCE = {
    "wechat": "wechat",
    "qq": "qq",
    "cc": "claude_code",
    "claude_code": "claude_code",
    "email": "email",
    "browser": "browser_session",
}


def _cache_path(source: str) -> Path:
    return _CACHE_DIR / f"{source}.json"


def _load_cache(source: str, ttl_sec: int) -> set[str] | None:
    p = _cache_path(source)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    fetched = float(data.get("fetched_ts", 0))
    age = time.time() - fetched
    if age > ttl_sec:
        return None
    bids = data.get("bids", [])
    return set(bids)


def _save_cache(source: str, bids: set[str], ttl_sec: int) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_ts": time.time(),
        "ttl_sec": ttl_sec,
        "bids": sorted(bids),
        "n": len(bids),
        "source": source,
        "pg_source": _DRIVER_TO_PG_SOURCE.get(source, source),
    }
    _cache_path(source).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


_SQL_TEMPLATE = (
    "SELECT DISTINCT metadata->>'batch_id' "
    "FROM memory_units "
    "WHERE bank_id=%(bank)s "
    "  AND metadata->>'source'=%(source)s "
    "  AND metadata->>'batch_id' IS NOT NULL"
)


def _query_pg_direct(pg_source: str, timeout: int = 60) -> set[str]:
    """Query PG via psycopg2 (preferred — no ssh shell-out)."""
    try:
        import psycopg2  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "psycopg2 not installed; install with `pip install psycopg2-binary` "
            "or set MEMEXA_PG_SSH_TARGET to use the ssh fallback"
        ) from exc

    if _PG_DSN:
        conn = psycopg2.connect(_PG_DSN, connect_timeout=timeout)
    else:
        conn = psycopg2.connect(
            host=_PG_HOST, port=_PG_PORT, user=_PG_USER,
            dbname=_PG_DB, connect_timeout=timeout,
        )
    try:
        with conn.cursor() as cur:
            cur.execute(_SQL_TEMPLATE, {"bank": _PG_BANK, "source": pg_source})
            return {row[0].strip() for row in cur.fetchall() if row[0]}
    finally:
        conn.close()


def _query_pg_via_ssh(pg_source: str, timeout: int = 60) -> set[str]:
    """Query PG via ssh+psql (fallback when port is firewalled)."""
    # Use ANSI quoting (E'...') to keep predictable shell-quoting.
    sql = _SQL_TEMPLATE.replace(
        "%(bank)s", f"'{_PG_BANK}'"
    ).replace(
        "%(source)s", f"'{pg_source}'"
    )
    cmd = [
        "ssh", _SSH_TARGET,
        f"{_PSQL_BIN} -h {_PG_HOST} -p {_PG_PORT} -U {_PG_USER} -d {_PG_DB} "
        f"-t -A -c \"{sql}\"",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"psql fail for {pg_source}: rc={proc.returncode} stderr={proc.stderr[:300]}"
        )
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _query_pg(pg_source: str, timeout: int = 60) -> set[str]:
    """Dispatch to direct or ssh transport based on env."""
    if _SSH_TARGET:
        return _query_pg_via_ssh(pg_source, timeout=timeout)
    return _query_pg_direct(pg_source, timeout=timeout)


def query_pg_existing_bids(
    source: str,
    ttl_sec: int | None = None,
    force_refresh: bool = False,
) -> set[str]:
    """Return set of batch_ids already in PG memory_full_v5 for this source.

    Cached on-disk with TTL (default 1h). Cache miss → SSH+psql + write.
    On PG-unreachable failure: returns last cached set if any, else empty set
    with stderr warning (graceful degradation; driver falls back to marker check).
    """
    ttl = ttl_sec if ttl_sec is not None else _DEFAULT_TTL
    pg_source = _DRIVER_TO_PG_SOURCE.get(source, source)

    if not force_refresh:
        cached = _load_cache(source, ttl)
        if cached is not None:
            return cached

    try:
        bids = _query_pg(pg_source)
    except Exception as exc:
        sys.stderr.write(f"[pg_bid_cache] WARN: query fail for {source}: {exc}\n")
        # Try to fall back to stale cache (better than empty)
        stale = _load_cache(source, ttl_sec=10**9)  # ignore TTL
        if stale is not None:
            sys.stderr.write(f"[pg_bid_cache] using stale cache n={len(stale)}\n")
            return stale
        return set()

    _save_cache(source, bids, ttl)
    return bids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source",
                    help="wechat | qq | cc | email | browser | all")
    ap.add_argument("--force-refresh", action="store_true")
    ap.add_argument("--ttl-sec", type=int, default=_DEFAULT_TTL)
    args = ap.parse_args()

    sources = (
        ["wechat", "qq", "cc", "email", "browser"]
        if args.source == "all"
        else [args.source]
    )
    for src in sources:
        t0 = time.time()
        bids = query_pg_existing_bids(src, args.ttl_sec, args.force_refresh)
        dt = time.time() - t0
        cache_status = "fresh" if dt > 0.5 else "cached"
        print(f"{src}: n={len(bids)} [{cache_status} in {dt:.2f}s]")


if __name__ == "__main__":
    main()
