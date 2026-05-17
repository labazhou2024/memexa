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
    """Create ``~/.memexa/`` with example config files, or onboard a source.

    v0.1.1: ``memexa init <source>`` dispatches to interactive wizards
    so first-time users do not have to hand-edit ``identity.yaml``:
      - ``memexa init email``   -> IMAP wizard with 6-provider auto-detect
      - ``memexa init wechat``  -> WeChatMsg-export onboarding (Win-only)
      - ``memexa init`` (no arg) -> legacy scaffold (write example yaml files)
    """
    source = getattr(args, "source", None)
    if source == "email":
        from memexa.cli.wizards import init_email_wizard
        return init_email_wizard(args)
    if source == "wechat":
        from memexa.cli.wizards import init_wechat_wizard
        return init_wechat_wizard(args)
    if source == "llm":
        from memexa.cli.wizards import init_llm_wizard
        return init_llm_wizard(args)
    if source is not None:
        print(f"[fail] unknown init source: {source!r}", file=sys.stderr)
        print( "       valid: email, wechat, llm (or no arg for legacy scaffold)",
               file=sys.stderr)
        return 2

    target = (
        Path(args.target).expanduser()
        if args.target
        else Path(os.environ.get("MEMEXA_CONFIG_DIR", str(Path.home() / ".memexa"))).expanduser()
    )
    target.mkdir(parents=True, exist_ok=True)

    # v0.1.1 (post §C.-41): templates ship under memexa/templates/ inside
    # the wheel. Repo-checkout layout (config/*.yaml + .env.example) is the
    # fallback for editable installs. Try packaged first, fall back to repo.
    here = Path(__file__).resolve().parents[2]
    pkg_templates = Path(__file__).resolve().parent.parent / "templates"
    examples = {
        target / "aliases.yaml": [
            pkg_templates / "aliases.example.yaml",
            here / "config" / "aliases.example.yaml",
        ],
        target / "identity.yaml": [
            pkg_templates / "identity.example.yaml",
            here / "config" / "identity.example.yaml",
        ],
        target / ".env": [
            pkg_templates / "env.example",
            here / ".env.example",
        ],
    }

    created = []
    skipped = []
    for dst, src_candidates in examples.items():
        src = next((s for s in src_candidates if s.exists()), None)
        if src is None:
            print(f"  [warn] template missing for {dst.name}; tried: "
                  f"{[str(s) for s in src_candidates]}", file=sys.stderr)
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
    print( "  1. Onboard an LLM provider:   memexa init llm")
    print( "  2. Onboard a source:          memexa init email   "
           "(or wechat)")
    print( "  3. Start backend:             memexa backend up")
    print( "  4. Ingest:                    memexa ingest email "
           "(or wechat)")
    print( "  5. Query:                     memexa quick \"test\"")
    print()
    print(f"Edit-by-hand alternative: open {target} and tweak")
    print(f"  aliases.yaml / identity.yaml / .env directly.")
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
            print("  - Run `memexa backend up` to start the local backend "
                  "(or `make backend-up` from a source checkout)")
            print("  - Set MEMEXA_HINDSIGHT_URL=http://your-host:8888 in ~/.memexa/.env")
            return rc

    # 2) Bank stats. v0.1.1: Hindsight reports the canonical count as
    # ``total_nodes`` (we previously read a non-existent ``nodes`` field,
    # so this always printed 0 even on a populated bank).
    try:
        r = httpx.get(f"{url}/v1/default/banks/{bank}/stats", timeout=5.0)
        if r.status_code == 200:
            data = r.json()
            n_nodes = data.get("total_nodes", data.get("nodes", 0))
            n_docs = data.get("total_documents", 0)
            n_links = data.get("total_links", 0)
            print(f"[ok]   bank '{bank}' has {n_nodes} nodes "
                  f"({n_docs} documents, {n_links} links)")
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
    """Probe backend health.

    v0.1.1: Hindsight's healthcheck path is ``/health`` (vectorize-io
    spec), not the legacy ``/healthz``. Try the modern path first, fall
    back to ``/healthz`` so this also works against any older Hindsight
    fork still in the wild.
    """
    for path in ("/health", "/healthz"):
        try:
            r = httpx_module.get(f"{url}{path}", timeout=3.0)
            if r.status_code == 200:
                print(f"[ok]   {label} {path} returned 200")
                return True
            if r.status_code == 404 and path == "/health":
                # try /healthz fallback once
                continue
            print(f"[warn] {label} {path} returned {r.status_code}")
            return False
        except Exception as e:
            print(f"[fail] {label} cannot reach {url}: {type(e).__name__}: {e}")
            return False
    print(f"[warn] {label} neither /health nor /healthz reachable")
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
    # 2026-05-17 v0.1.1: previous code prefixed an extra ``/v1`` even when
    # the user already wrote ``.../v1`` into ``MEMEXA_REMOTE_LLM_BASE_URL``
    # (the convention used by USTC LiteLLM, DeepSeek, OpenAI, and most
    # OpenAI-compatible endpoints). Result: ``/v1/v1/chat/completions``
    # → 404 even when the model was reachable. Detect-and-skip.
    endpoint = f"{base}/v1/chat/completions"
    if base.endswith("/v1") or "/v1/" in base:
        endpoint = f"{base}/chat/completions"
    try:
        r = httpx_module.post(
            endpoint,
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


def _cmd_ingest(args: argparse.Namespace) -> int:
    """Dispatch ``memexa ingest <source>`` to the per-source wrapper."""
    source = args.source
    if source == "email":
        from memexa.cli.wizards import ingest_email
        return ingest_email(args)
    if source == "wechat":
        from memexa.cli.wizards import ingest_wechat
        return ingest_wechat(args)
    print(f"[fail] unknown ingest source: {source!r}", file=sys.stderr)
    return 2


def _cmd_backend(args: argparse.Namespace) -> int:
    """Dispatch ``memexa backend <up|down|status>``."""
    action = args.action
    if action == "up":
        from memexa.cli.wizards import backend_up
        return backend_up(args)
    if action == "down":
        from memexa.cli.wizards import backend_down
        return backend_down(args)
    if action == "status":
        from memexa.cli.wizards import backend_status
        return backend_status(args)
    print(f"[fail] unknown backend action: {action!r}", file=sys.stderr)
    return 2


def _cmd_demo(_args: argparse.Namespace) -> int:
    """30-second onboarding: ingest the bundled synthetic dataset with the
    stub extractor (no LLM key required), then run five example queries
    against the resulting in-memory card set.

    No Docker, no API key, no configuration. Designed to give a first-
    time visitor a concrete sense of what the project does in well under
    a minute on a clean Python install.
    """
    # 2026-05-16: demo output uses non-ASCII glyphs (✓, ─, ▸, en/em
    # dashes, CJK). Windows console default is GBK which can't encode
    # any of those. Force UTF-8 the same way memory_query._cli does.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("memexa demo  —  thirty-second onboarding")
    print("─" * 60)
    print("[1/3] Ingesting the bundled synthetic dataset (stub extractor) ...")

    try:
        from examples.demo_dataset import ingest as demo_ingest  # type: ignore
    except Exception as e:  # pragma: no cover — defensive
        print(f"[fail] cannot import bundled demo dataset: {e}", file=sys.stderr)
        print("       Ensure memexa was installed from a source distribution"
              " that includes the examples/ tree, or clone the repo.",
              file=sys.stderr)
        return 1

    cards: list[dict] = []
    try:
        for source_fn in (
            demo_ingest.ingest_wechat,
            demo_ingest.ingest_qq,
            demo_ingest.ingest_email,
            demo_ingest.ingest_browser,
            demo_ingest.ingest_claude,
            demo_ingest.ingest_audio,
        ):
            cards.extend(source_fn(stub=True))
    except AttributeError:
        # Older demo dataset module shape: call top-level ingest_all().
        cards = demo_ingest.ingest_all(stub=True)  # type: ignore[attr-defined]

    if not cards:
        print("[fail] demo ingestion produced zero cards — bundle malformed.",
              file=sys.stderr)
        return 1

    by_src: dict[str, int] = {}
    for c in cards:
        by_src[c.get("source", "?")] = by_src.get(c.get("source", "?"), 0) + 1
    src_summary = ", ".join(f"{s}={n}" for s, n in sorted(by_src.items()))
    print(f"      ✓ Ingested {len(cards)} cards across {len(by_src)}"
          f" sources ({src_summary}).")
    print()

    print("[2/3] Running five sample queries against the in-memory set ...")
    samples = [
        ("quick", "Alice",
         lambda kw: [c for c in cards
                     if kw.lower() in c.get("narrative", "").lower()
                     or kw.lower() in " ".join(
                         e.get("surface", "") for e in c.get("entities", [])
                     ).lower()][:3]),
        ("arc", "Alice ↔ Bob",
         lambda _kw: [c for c in cards
                      if "Alice" in c.get("narrative", "")
                      and "Bob" in c.get("narrative", "")][:3]),
        ("timeline", "2024-01",
         lambda _kw: sorted(
             [c for c in cards if c.get("when_start", "").startswith("2024-01")],
             key=lambda x: x.get("when_start", ""),
         )[:3]),
        ("pending", "(commitment cards)",
         lambda _kw: [c for c in cards
                      if "commitment" in c.get("types_csv", "")][:3]),
        ("topic", "DDIA",
         lambda kw: [c for c in cards
                     if kw.lower() in c.get("narrative", "").lower()][:3]),
    ]

    for sub, term, fn in samples:
        try:
            hits = fn(term)
        except Exception:  # pragma: no cover
            hits = []
        print(f"  ▸ memexa {sub} {term!r}")
        if not hits:
            print(f"     (0 cards — synthetic dataset; expected for some samples)")
        for h in hits:
            narr = h.get("narrative", "")[:90]
            when = h.get("when_start", "?")[:10]
            src = h.get("source", "?")
            print(f"     [{src:<7s} {when}] {narr}")
        print()

    print("[3/3] Done.  Next steps:")
    print("      • memexa init       — scaffold ~/.memexa/ config")
    print("      • memexa doctor     — self-diagnostic against your backend")
    print("      • docs/quickstart.md — Tier 1 (5 min) or Tier 2 (30 min)")
    return 0


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
        help="scaffold ~/.memexa/ config (no arg) or onboard a source (email/wechat)",
    )
    sp_init.add_argument(
        "source",
        nargs="?",
        default=None,
        choices=["email", "wechat", "llm"],
        help="source to onboard (email/wechat) or component to set up (llm); "
             "omit for legacy yaml scaffold",
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

    # v0.1.1: top-level `ingest <source>` -- wraps the per-source builders
    # so users do not have to call `python -m memexa.extraction.<...>` by hand.
    sp_ingest = sub.add_parser(
        "ingest",
        help="ingest a configured source (email/wechat)",
    )
    sp_ingest.add_argument(
        "source",
        choices=["email", "wechat"],
        help="which source to ingest",
    )
    sp_ingest.add_argument(
        "--account", default=None,
        help="(email only) account name from identity.yaml; default: all",
    )
    sp_ingest.add_argument(
        "--since", default=None,
        help="(email only) ISO date YYYY-MM-DD; default: use account's since_days",
    )
    sp_ingest.add_argument(
        "--max-per-folder", type=int, default=None,
        help="(email only) cap per IMAP folder, default unlimited",
    )
    sp_ingest.add_argument(
        "--from", dest="from_dir", default=None,
        help="(wechat only) WeChatMsg export directory; "
             "default: wechat.export_dir from identity.yaml",
    )
    sp_ingest.set_defaults(func=_cmd_ingest)

    # v0.1.1: top-level `backend` -- docker-compose wrapper for the
    # bundled pg+Hindsight stack. Users get a one-liner instead of
    # having to memorise the compose file path.
    sp_backend = sub.add_parser(
        "backend",
        help="bring up / tear down / check the Docker backend (pg + Hindsight)",
    )
    sp_backend.add_argument(
        "action",
        choices=["up", "down", "status"],
        help="up = start; down = stop; status = container state + healthz",
    )
    sp_backend.set_defaults(func=_cmd_backend)

    sp_config = sub.add_parser("config", help="print resolved configuration")
    sp_config.set_defaults(func=_cmd_config)

    sp_doctor = sub.add_parser("doctor", help="self-diagnostic against backend")
    sp_doctor.set_defaults(func=_cmd_doctor)

    sp_demo = sub.add_parser(
        "demo",
        help="30-second onboarding: ingest synthetic dataset + run 5 queries (no backend, no LLM key)",
    )
    sp_demo.set_defaults(func=_cmd_demo)

    # `memexa query <subcmd>` proxies to memory_query
    sp_query = sub.add_parser(
        "query",
        help="run a memory_query subcommand (quick/topic/arc/...)",
        add_help=False,  # forward --help to memory_query
    )
    sp_query.set_defaults(func=None)  # special-cased below

    return p


def _force_utf8_stdio() -> None:
    """Force UTF-8 stdout/stderr.

    Windows default code page is often GBK (cp936 on Chinese locales),
    cp437/cp1252 elsewhere. memexa prints Chinese characters, the ¥
    symbol, em-dashes, etc. throughout the wizards and demo output,
    and the wizards take user input that may contain Chinese names.
    Without this we crash on a vanilla Win 11 console
    (UnicodeEncodeError: 'gbk' codec can't encode character '\\xa5').

    Python 3.7+ exposes ``sys.stdout.reconfigure`` on TextIO wrappers
    that are real ``TextIOWrapper`` instances (not subprocess pipes or
    test capture streams). Best-effort: if reconfigure isn't available
    or fails, leave stdio alone — the original error path still shows
    a useful traceback.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point installed as ``memexa`` and ``memexa-query``.

    Returns an exit code (0 = success). Friendly fallbacks for common errors.
    """
    _force_utf8_stdio()
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
