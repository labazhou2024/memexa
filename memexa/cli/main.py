"""memexa unified CLI dispatcher.

Subcommands:
  init       Scaffold ``~/.memexa/`` config directory with example files.
  version    Print memexa version + key dependency versions.
  config     Print resolved configuration (env vars + yaml files).
  query      Delegate to :mod:`memexa.core.memory_query` (14 subcommands).
  doctor     Run a self-diagnostic against the configured backend.

The classic ``memexa-query`` console-script is an alias for ``memexa query``
to preserve back-compat for anyone scripting around the old name.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional


# Subcommands listed by ``memory_query._cli`` — used for back-compat dispatch
# when the user runs ``memexa quick "X"`` instead of ``memexa query quick "X"``.
_QUERY_SUBCMDS = {
    "quick",
    "topic",
    "arc",
    "timeline",
    "person",
    "project",
    "pending",
    "reflect",
    "session-context",
    "types",
    "graph-walk",
    "summary",
    "trends",
    "cross-source",
}


def _cmd_version(_args: argparse.Namespace) -> int:
    """Print memexa + Python + key-dep versions."""
    try:
        from memexa import __version__ as memexa_version
    except Exception:
        memexa_version = "unknown"

    print(f"memexa  {memexa_version}")
    print(f"python {sys.version.split()[0]}  ({sys.platform})")

    interesting = [
        "httpx",
        "pydantic",
        "fastapi",
        "uvicorn",
        "psycopg2-binary",
        "numpy",
        "rich",
        "hindsight-client",
    ]
    print()
    for name in interesting:
        try:
            from importlib.metadata import version  # py3.8+
            v = version(name)
            print(f"  {name:<20} {v}")
        except Exception:
            print(f"  {name:<20} (not installed)")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    """Create ``~/.memexa/`` with example config files.

    Resolution order matches ``memexa.core._path_resolver``:
      1. ``--target`` flag
      2. ``MEMEXA_CONFIG_DIR`` env var
      3. ``~/.memexa``
    """
    target = (
        Path(args.target).expanduser()
        if args.target
        else Path(os.environ.get("MEMEXA_CONFIG_DIR", str(Path.home() / ".memexa"))).expanduser()
    )
    target.mkdir(parents=True, exist_ok=True)

    here = Path(__file__).resolve().parents[2]
    examples = {
        here / "config" / "aliases.example.yaml": target / "aliases.yaml",
        here / "config" / "identity.example.yaml": target / "identity.yaml",
        here / ".env.example": target / ".env",
    }

    created = []
    skipped = []
    for src, dst in examples.items():
        if not src.exists():
            print(f"  [warn] template missing: {src}", file=sys.stderr)
            continue
        if dst.exists() and not args.force:
            skipped.append(dst)
            continue
        shutil.copy2(src, dst)
        created.append(dst)

    print(f"memexa init -> {target}")
    for p in created:
        print(f"  created  {p.name}")
    for p in skipped:
        print(f"  exists   {p.name}  (use --force to overwrite)")

    print()
    print("Next steps:")
    print(f"  1. Edit {target / 'aliases.yaml'} — list your self-aliases")
    print(f"  2. Edit {target / 'identity.yaml'} — set qq_id / primary_email")
    print(f"  3. Edit {target / '.env'}         — point at your backend")
    print("  4. Start backend:   make backend-up")
    print("  5. Ingest demo:     make demo-ingest")
    print("  6. Query:           memexa quick \"test\"")
    return 0


def _cmd_config(_args: argparse.Namespace) -> int:
    """Print resolved configuration (env vars + file paths)."""
    print("=== memexa configuration ===")
    print()
    print("Environment variables (MEMEXA_*):")
    env_vars = sorted(k for k in os.environ if k.startswith("MEMEXA_"))
    if env_vars:
        for k in env_vars:
            v = os.environ[k]
            # truncate long values, mask anything that smells secret
            if any(s in k.lower() for s in ("key", "token", "password", "secret")):
                v = v[:4] + "***" if v else "(empty)"
            print(f"  {k:<35} = {v}")
    else:
        print("  (none set)")

    print()
    print("Config files:")
    home_cfg = Path.home() / ".memexa"
    for name in ("aliases.yaml", "identity.yaml", "config.yaml", ".env"):
        p = home_cfg / name
        status = "present" if p.exists() else "missing"
        print(f"  {str(p):<60}  {status}")

    print()
    print("Resolved paths (via memexa.core._path_resolver):")
    try:
        from memexa.core._path_resolver import (
            workspace_root,
            memory_dir,
            data_dir,
            logs_dir,
        )
        print(f"  workspace_root() -> {workspace_root()}")
        print(f"  memory_dir()     -> {memory_dir()}")
        print(f"  data_dir()       -> {data_dir()}")
        print(f"  logs_dir()       -> {logs_dir()}")
    except Exception as e:
        print(f"  (error: {type(e).__name__}: {e})")

    print()
    print("Backend:")
    url = os.environ.get("MEMEXA_HINDSIGHT_URL", "http://127.0.0.1:8888")
    bank = os.environ.get("MEMEXA_HINDSIGHT_BANK", "memory_full_v5")
    print(f"  URL  = {url}")
    print(f"  Bank = {bank}")
    return 0


def _cmd_doctor(_args: argparse.Namespace) -> int:
    """Self-diagnostic: backend reachable, bank exists, LLM provider responds."""
    url = os.environ.get("MEMEXA_HINDSIGHT_URL", "http://127.0.0.1:8888")
    fallback_url = os.environ.get("MEMEXA_HINDSIGHT_FALLBACK_URL", "").strip()
    bank = os.environ.get("MEMEXA_HINDSIGHT_BANK", "memory_full_v5")

    print("memexa doctor")
    print()
    print(f"Target backend: {url}")
    if fallback_url:
        print(f"Fallback:       {fallback_url}")
    print(f"Target bank:    {bank}")
    print()

    try:
        import httpx  # type: ignore
    except ImportError:
        print("[fail] httpx not installed — run: pip install memexa")
        return 2

    rc = 0

    # 1) Backend health (primary, then fallback if any)
    primary_ok = _probe_health(httpx, url, label="primary")
    if not primary_ok:
        rc = 1
        if fallback_url:
            if _probe_health(httpx, fallback_url, label="fallback"):
                print("[warn] primary down, fallback up — failover will engage")
                rc = 0  # functionally available
        if rc != 0:
            print()
            print("Hints:")
            print("  - Run `make backend-up` to start the local backend")
            print("  - Set MEMEXA_HINDSIGHT_URL=http://your-host:8888 in ~/.memexa/.env")
            return rc

    # 2) Bank stats
    try:
        r = httpx.get(f"{url}/v1/default/banks/{bank}/stats", timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            n = data.get("nodes", 0)
            print(f"[ok]   bank '{bank}' has {n} nodes")
        else:
            print(f"[warn] bank stats returned {r.status_code} — bank may not exist yet")
    except Exception as e:
        print(f"[warn] bank stats probe failed: {type(e).__name__}: {e}")

    # 3) LLM provider (gatekeeper + extractor) round-trip
    print()
    rc |= _probe_llm(httpx, role="gate")
    rc |= _probe_llm(httpx, role="extract")

    # 4) Identity manifest
    print()
    home_cfg = Path.home() / ".memexa"
    for name in ("aliases.yaml", "identity.yaml"):
        p = home_cfg / name
        if p.exists():
            print(f"[ok]   {name} present")
        else:
            print(f"[warn] {name} missing — run `memexa init`")

    return rc


def _probe_health(httpx_module, url: str, *, label: str) -> bool:
    try:
        r = httpx_module.get(f"{url}/healthz", timeout=3.0)
        if r.status_code == 200:
            print(f"[ok]   {label} /healthz returned 200")
            return True
        print(f"[warn] {label} /healthz returned {r.status_code}")
        return False
    except Exception as e:
        print(f"[fail] {label} cannot reach {url}: {type(e).__name__}: {e}")
        return False


def _probe_llm(httpx_module, *, role: str) -> int:
    """Round-trip the configured chat-completions endpoint for one role.

    Returns 0 on success or skip (no env), 1 on fail. `role` is `gate` or
    `extract`; both reuse the same base URL/key, only the model name
    differs.
    """
    base = os.environ.get("MEMEXA_REMOTE_LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("MEMEXA_REMOTE_LLM_API_KEY", "")
    model = os.environ.get(
        "MEMEXA_REMOTE_LLM_GATE_MODEL" if role == "gate" else "MEMEXA_REMOTE_LLM_EXTRACT_MODEL",
        "",
    )
    if not (base and model):
        print(f"[skip] LLM/{role}: MEMEXA_REMOTE_LLM_BASE_URL or model not set")
        return 0
    try:
        r = httpx_module.post(
            f"{base}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 4,
                "temperature": 0,
            },
            headers={"Authorization": f"Bearer {key}"} if key else {},
            timeout=10.0,
        )
        if r.status_code == 200:
            print(f"[ok]   LLM/{role} ({model}) responded 200")
            return 0
        print(f"[fail] LLM/{role} ({model}) returned {r.status_code}: {r.text[:120]}")
        return 1
    except Exception as e:
        print(f"[fail] LLM/{role} ({model}) probe error: {type(e).__name__}: {e}")
        return 1


def _cmd_query(args: argparse.Namespace, remainder: List[str]) -> int:
    """Delegate to :func:`memexa.core.memory_query._cli`."""
    try:
        from memexa.core import memory_query  # type: ignore
    except Exception as e:
        print(f"[fail] cannot import memexa.core.memory_query: {e}", file=sys.stderr)
        return 1
    # Forward as if invoked via ``python -m memexa.core.memory_query <subcmd> ...``
    argv = ["memexa.core.memory_query"] + remainder
    return memory_query._cli(argv)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memexa",
        description="memexa — self-hosted Chinese personal memory graph",
    )
    p.add_argument(
        "--version",
        action="store_true",
        help="print version and exit",
    )
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    sp_version = sub.add_parser("version", help="print memexa + dep versions")
    sp_version.set_defaults(func=_cmd_version)

    sp_init = sub.add_parser(
        "init",
        help="scaffold ~/.memexa/ config directory",
    )
    sp_init.add_argument(
        "--target",
        type=str,
        default=None,
        help="config dir (default: $MEMEXA_CONFIG_DIR or ~/.memexa)",
    )
    sp_init.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing files",
    )
    sp_init.set_defaults(func=_cmd_init)

    sp_config = sub.add_parser("config", help="print resolved configuration")
    sp_config.set_defaults(func=_cmd_config)

    sp_doctor = sub.add_parser("doctor", help="self-diagnostic against backend")
    sp_doctor.set_defaults(func=_cmd_doctor)

    # `memexa query <subcmd>` proxies to memory_query
    sp_query = sub.add_parser(
        "query",
        help="run a memory_query subcommand (quick/topic/arc/...)",
        add_help=False,  # forward --help to memory_query
    )
    sp_query.set_defaults(func=None)  # special-cased below

    return p


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point installed as ``memexa`` and ``memexa-query``.

    Returns an exit code (0 = success). Friendly fallbacks for common errors.
    """
    raw = list(argv if argv is not None else sys.argv[1:])

    # Top-level --version shortcut
    if raw and raw[0] in ("--version", "-V"):
        return _cmd_version(argparse.Namespace())

    # Back-compat: `memexa quick "X"` (no `query` prefix) -> delegate to memory_query
    if raw and raw[0] in _QUERY_SUBCMDS:
        return _cmd_query(argparse.Namespace(), raw)

    parser = _build_parser()
    args, remainder = parser.parse_known_args(raw)

    if args.version:
        return _cmd_version(args)

    if args.cmd == "query":
        return _cmd_query(args, remainder)

    if args.cmd is None:
        parser.print_help()
        print()
        print("Hint: try `memexa init` to scaffold config, or `memexa quick \"X\"` for a query.")
        return 0

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2

    try:
        return func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"[fail] {type(e).__name__}: {e}", file=sys.stderr)
        if os.environ.get("MEMEXA_DEBUG"):
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
