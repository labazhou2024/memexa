"""memexa — open demo CLI.

This is the **open-source demo** of memexa, a self-hosted Chinese
personal memory graph. It ships a small synthetic dataset and a stub
extractor so that, in about thirty seconds and with no backend and no
API key, you can see what the project does.

The full memexa engine — live ingestion from WeChat / QQ / email /
documents / audio, the multi-channel recall stack, the MCP server, and
the desktop application — is the proprietary memexa product. See the
README for what the full engine adds and how to get access.

Commands:
  demo       Ingest the bundled synthetic dataset, then run sample queries.
  version    Print the memexa + Python versions.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

_PROPRIETARY_NOTICE = (
    "This is the open memexa demo (synthetic data + stub extractor).\n"
    "The full engine — live ingestion, the recall stack, the MCP server,\n"
    "and the desktop app — is the proprietary memexa product. See the\n"
    "README for what it adds and how to get access."
)


def _force_utf8_stdio() -> None:
    """Force UTF-8 stdout/stderr.

    The demo prints non-ASCII glyphs (✓, ─, ▸, en/em dashes, CJK). The
    Windows console default code page (often GBK on Chinese locales)
    cannot encode them and would raise UnicodeEncodeError. Best-effort:
    if ``reconfigure`` is unavailable (e.g. a piped/captured stream),
    leave the stream alone.
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


def _cmd_version(_args: argparse.Namespace) -> int:
    """Print the memexa + Python versions."""
    try:
        from memexa import __version__ as memexa_version
    except Exception:
        memexa_version = "unknown"

    print(f"memexa  {memexa_version}  (open demo)")
    print(f"python  {sys.version.split()[0]}  ({sys.platform})")
    return 0


def _cmd_demo(_args: argparse.Namespace) -> int:
    """30-second onboarding: ingest the bundled synthetic dataset with the
    stub extractor (no LLM key required), then run five sample queries
    against the resulting in-memory card set.

    No Docker, no API key, no configuration — a concrete first look at
    what the project does, on a clean Python install.
    """
    _force_utf8_stdio()
    print("memexa demo  —  thirty-second onboarding")
    print("─" * 60)
    print("[1/3] Ingesting the bundled synthetic dataset (stub extractor) ...")

    try:
        from examples.demo_dataset import ingest as demo_ingest  # type: ignore
    except Exception as e:  # pragma: no cover — defensive
        print(f"[fail] cannot import bundled demo dataset: {e}", file=sys.stderr)
        print(
            "       Ensure memexa was installed from a source distribution"
            " that includes the examples/ tree, or clone the repo.",
            file=sys.stderr,
        )
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
        print(
            "[fail] demo ingestion produced zero cards — bundle malformed.",
            file=sys.stderr,
        )
        return 1

    by_src: dict[str, int] = {}
    for c in cards:
        by_src[c.get("source", "?")] = by_src.get(c.get("source", "?"), 0) + 1
    src_summary = ", ".join(f"{s}={n}" for s, n in sorted(by_src.items()))
    print(
        f"      ✓ Ingested {len(cards)} cards across {len(by_src)}"
        f" sources ({src_summary})."
    )
    print()

    print("[2/3] Running five sample queries against the in-memory set ...")
    samples = [
        (
            "quick",
            "Alice",
            lambda kw: [
                c
                for c in cards
                if kw.lower() in c.get("narrative", "").lower()
                or kw.lower()
                in " ".join(e.get("surface", "") for e in c.get("entities", [])).lower()
            ][:3],
        ),
        (
            "arc",
            "Alice ↔ Bob",
            lambda _kw: [
                c
                for c in cards
                if "Alice" in c.get("narrative", "") and "Bob" in c.get("narrative", "")
            ][:3],
        ),
        (
            "timeline",
            "2024-01",
            lambda _kw: sorted(
                [c for c in cards if c.get("when_start", "").startswith("2024-01")],
                key=lambda x: x.get("when_start", ""),
            )[:3],
        ),
        (
            "pending",
            "(commitment cards)",
            lambda _kw: [c for c in cards if "commitment" in c.get("types_csv", "")][:3],
        ),
        (
            "topic",
            "DDIA",
            lambda kw: [
                c for c in cards if kw.lower() in c.get("narrative", "").lower()
            ][:3],
        ),
    ]

    for sub, term, fn in samples:
        try:
            hits = fn(term)
        except Exception:  # pragma: no cover
            hits = []
        print(f"  ▸ memexa {sub} {term!r}")
        if not hits:
            print("     (0 cards — synthetic dataset; expected for some samples)")
        for h in hits:
            narr = h.get("narrative", "")[:90]
            when = h.get("when_start", "?")[:10]
            src = h.get("source", "?")
            print(f"     [{src:<7s} {when}] {narr}")
        print()

    print("[3/3] Done. That was the stub extractor on synthetic data.")
    print("      The full memexa engine — live ingestion, the recall stack,")
    print("      the MCP server, and the desktop app — is the proprietary")
    print("      product. See the README for what it adds and how to get access.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memexa",
        description="memexa — open demo of a self-hosted Chinese memory graph",
        epilog=_PROPRIETARY_NOTICE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--version",
        action="store_true",
        help="print version and exit",
    )
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    sp_version = sub.add_parser("version", help="print memexa + dependency versions")
    sp_version.set_defaults(func=_cmd_version)

    sp_demo = sub.add_parser(
        "demo",
        help="30-second onboarding: ingest synthetic dataset + run 5 queries "
        "(no backend, no LLM key)",
    )
    sp_demo.set_defaults(func=_cmd_demo)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point installed as the ``memexa`` console script."""
    _force_utf8_stdio()
    raw = list(argv if argv is not None else sys.argv[1:])

    if raw and raw[0] in ("--version", "-V"):
        return _cmd_version(argparse.Namespace())

    parser = _build_parser()
    args = parser.parse_args(raw)

    if getattr(args, "version", False):
        return _cmd_version(args)

    if args.cmd is None:
        parser.print_help()
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
