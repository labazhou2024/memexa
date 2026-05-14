"""
Subprocess launcher for `claude` CLI — Windows-safe.

Resolves the `claude` npm-installed CLI to an absolute path at import time
so `subprocess.run(shell=False)` works across Windows / Linux / macOS.

Problem (Windows): `subprocess.run(['claude', '-p', ...], shell=False)` raises
`FileNotFoundError [WinError 2]` because `CreateProcessW` does NOT consult
PATHEXT. The npm global install ships `claude.CMD`, not bare `claude`.

Fix (industry standard, cited in chief-researcher report 2026-04-20):
1. `shutil.which('claude')` at module import — honors PATHEXT, returns `.CMD`
2. Validate against allowlist (APPDATA/npm on Windows) to defeat PATH hijack
3. Cache `CLAUDE_BIN` as module constant
4. Callers pass it as argv[0] with `shell=False`

Security: per architect arbitration, per-call re-resolution does NOT defeat
the pre-start PATH-poisoning threat (attacker who can write user PATH can
also write env_overrides.json, memory/, or Python source). Import-time
allowlist is the right defense for bpo-33515 PATHEXT quirks.

Callers: memory_ingest_watcher, governance_middleware, session_reflector,
soft_signal_classifier, benchmark/bce_demos/*.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence

__all__ = ["CLAUDE_BIN", "run_claude", "claude_argv", "is_available"]


def _allowed_prefixes() -> tuple[Path, ...]:
    """Return prefixes under which the resolved `claude` binary must live.

    On Windows: `%APPDATA%\\npm` (npm global bin) + `%ProgramFiles%\\nodejs`.
    On Linux/macOS: common npm global dirs + `/usr/local/bin`.
    Empty tuple -> no validation (not recommended but harmless fallback).
    """
    if sys.platform == "win32":
        candidates = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "npm")
        program_files = os.environ.get("ProgramFiles")
        if program_files:
            candidates.append(Path(program_files) / "nodejs")
        return tuple(candidates)
    else:
        candidates = [Path("/usr/local/bin"), Path("/usr/bin")]
        # Path.home() raises RuntimeError in sandboxed envs without HOME.
        try:
            home = Path.home()
            candidates.extend([
                home / ".npm-global" / "bin",
                home / ".nvm",
                home / ".local" / "bin",
            ])
        except (RuntimeError, OSError):
            pass
        return tuple(candidates)


def _resolve_claude_bin() -> Optional[str]:
    """Resolve `claude` CLI to absolute path. Returns None if not found.

    Does NOT raise — callers decide whether missing CLI is fatal.
    Import-time allowlist validation: resolved path must live under one of
    the known-safe prefixes; otherwise treated as not-found.
    """
    found = shutil.which("claude")
    if not found:
        return None

    try:
        resolved = Path(found).resolve()
    except (OSError, RuntimeError):
        return None

    if not resolved.is_file():
        return None

    prefixes = _allowed_prefixes()
    if prefixes:
        ok = False
        for prefix in prefixes:
            try:
                resolved.relative_to(prefix.resolve())
                ok = True
                break
            except (ValueError, OSError):
                continue
        if not ok:
            # Out-of-allowlist. Log via stderr (not logger — this is
            # import-time and logger may not be configured).
            sys.stderr.write(
                f"[subprocess_launcher] claude CLI at {resolved} is outside "
                f"allowed prefixes {[str(p) for p in prefixes]}; ignoring.\n"
            )
            return None

    return str(resolved)


# Module-level cache. Resolved once at first import.
CLAUDE_BIN: Optional[str] = _resolve_claude_bin()


def is_available() -> bool:
    """True if claude CLI is resolved and available."""
    return CLAUDE_BIN is not None


def claude_argv(args: Sequence[str]) -> List[str]:
    """Build argv with resolved CLAUDE_BIN as argv[0].

    Raises FileNotFoundError if CLI is missing — so callers who want to
    queue/retry can catch this exception (drain_queue et al. do).
    """
    if CLAUDE_BIN is None:
        raise FileNotFoundError(
            "claude CLI not found on PATH. Install via "
            "`npm i -g @anthropic-ai/claude-code` and restart the process."
        )
    return [CLAUDE_BIN, *args]


def run_claude(
    args: Sequence[str],
    *,
    input: Optional[str] = None,
    timeout: Optional[float] = None,
    capture_output: bool = True,
    text: bool = True,
    encoding: str = "utf-8",
    errors: str = "replace",
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Invoke `claude` CLI via absolute-path subprocess.run(shell=False).

    Raises:
      FileNotFoundError: CLI missing on PATH (callers may queue and retry).
      subprocess.TimeoutExpired: `timeout` exceeded.

    All other subprocess outcomes (non-zero exit, parse failures) are
    returned in CompletedProcess; caller interprets.
    """
    argv = claude_argv(args)
    return subprocess.run(
        argv,
        input=input,
        timeout=timeout,
        capture_output=capture_output,
        text=text,
        encoding=encoding,
        errors=errors,
        check=check,
        shell=False,
    )
