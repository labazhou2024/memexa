"""Probe schema + B1 security sandbox for integration_gate probes.

Probes are declared as YAML entries inside a `### integration_probes`
fenced block in plan_v*.md files. This module handles:

  - parsing (extract fenced YAML from markdown)
  - schema validation (required fields; correct types)
  - action field sandbox (B1 from plan_v3):
      * allowlist prefix match (python -m pytest, python memexa/scripts/,
        python -m src.core., python -c "<=200 chars>")
      * forbidden tokens (shell metacharacters, network tools, eval-ish)
      * ProbeSchemaError on any violation
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class ProbeSchemaError(ValueError):
    """Raised when a probe fails schema / sandbox validation."""


# B1 sandbox — allowlist prefixes. Order-insensitive; any must match.
_ACTION_ALLOWLIST_PREFIXES = (
    "python -m pytest ",
    "python memexa/scripts/",
    "python -m src.core.",
    "python -c \"",
)

# B1 sandbox — forbidden tokens (substring match, case-insensitive).
_ACTION_FORBIDDEN_TOKENS = (
    "`", "$(", "&&", "||", ";", "|", ">", "<",
    "curl", "wget", "nc ", "bash -c", "sh -c",
    "rm -rf", "eval", "exec", "setx",
)

# python -c "..." length cap (includes the whole action string)
_ACTION_PY_C_MAX_LEN = 200


_ASSERTION_CHANNELS = {"trace_event", "neo4j_delta", "file_mtime", "stdout_pattern"}


@dataclass
class Probe:
    id: str
    tu: str
    subject: str
    action: str
    timeout_s: int
    requires: List[str] = field(default_factory=list)
    downstream_assertion: Dict[str, Any] = field(default_factory=dict)
    encoding_assertion: bool = False


def _validate_action(action: str) -> None:
    """B1 sandbox — raise ProbeSchemaError if action fails validation.

    Stage 4 SEC-1+SEC-2 hardening:
      - NFC normalize before token check (defeats homoglyph bypass)
      - For `python memexa/scripts/` prefix, assert the script path
        resolves inside memexa scripts dir (reject parent-dir escapes)
    """
    if not isinstance(action, str) or not action.strip():
        raise ProbeSchemaError("action must be non-empty string")

    # SEC-2: NFC normalize before token scanning (Unicode homoglyph defense)
    import unicodedata as _ud
    s = _ud.normalize("NFC", action.strip())

    # Allowlist prefix check
    if not any(s.startswith(p) for p in _ACTION_ALLOWLIST_PREFIXES):
        raise ProbeSchemaError(
            f"action must start with one of {_ACTION_ALLOWLIST_PREFIXES}; got: {s[:60]!r}"
        )

    # SEC-1: for `python memexa/scripts/`, assert no `../` traversal
    if s.startswith("python memexa/scripts/"):
        # Extract the script path fragment (first token after prefix)
        tail = s[len("python memexa/scripts/"):]
        # Split on whitespace — first word is the script filename
        first_token = tail.split()[0] if tail.split() else ""
        # Traversal indicators
        if (".." in first_token) or first_token.startswith("/") or ":\\" in first_token:
            raise ProbeSchemaError(
                f"action path traversal rejected: {first_token!r}"
            )

    # python -c length cap
    if s.startswith("python -c \"") and len(s) > _ACTION_PY_C_MAX_LEN:
        raise ProbeSchemaError(
            f"python -c action too long ({len(s)} > {_ACTION_PY_C_MAX_LEN} chars)"
        )

    # Forbidden token check (case-insensitive, on normalized form)
    lower = s.lower()
    for tok in _ACTION_FORBIDDEN_TOKENS:
        if tok.lower() in lower:
            raise ProbeSchemaError(
                f"action contains forbidden token {tok!r}: {s[:80]!r}"
            )


def _validate_assertion(a: Dict[str, Any], probe_id: str) -> None:
    """Check downstream_assertion shape."""
    if not isinstance(a, dict) or not a:
        raise ProbeSchemaError(
            f"probe {probe_id}: downstream_assertion required (non-empty dict)"
        )
    ch = a.get("channel")
    if ch not in _ASSERTION_CHANNELS:
        raise ProbeSchemaError(
            f"probe {probe_id}: downstream_assertion.channel must be one of "
            f"{_ASSERTION_CHANNELS}; got {ch!r}"
        )
    # Per-channel required fields
    if ch == "trace_event" and "event_pattern" not in a and "event" not in a:
        raise ProbeSchemaError(
            f"probe {probe_id}: trace_event channel requires 'event_pattern' or 'event'"
        )
    if ch == "stdout_pattern" and "pattern" not in a:
        raise ProbeSchemaError(
            f"probe {probe_id}: stdout_pattern channel requires 'pattern'"
        )
    if ch == "file_mtime" and "path" not in a:
        raise ProbeSchemaError(
            f"probe {probe_id}: file_mtime channel requires 'path'"
        )
    if "within_s" not in a:
        raise ProbeSchemaError(
            f"probe {probe_id}: downstream_assertion.within_s required"
        )


def validate_probe_dict(raw: Dict[str, Any]) -> Probe:
    """Convert dict → Probe; raise ProbeSchemaError on any violation."""
    if not isinstance(raw, dict):
        raise ProbeSchemaError(f"probe must be dict; got {type(raw).__name__}")
    required = ("id", "tu", "subject", "action", "timeout_s",
                "downstream_assertion")
    missing = [k for k in required if k not in raw]
    if missing:
        raise ProbeSchemaError(f"missing required fields: {missing}")

    probe_id = str(raw["id"])
    _validate_action(str(raw["action"]))
    _validate_assertion(raw["downstream_assertion"], probe_id)

    ts = raw["timeout_s"]
    if not isinstance(ts, int) or ts <= 0 or ts > 300:
        raise ProbeSchemaError(
            f"probe {probe_id}: timeout_s must be int in (0,300]; got {ts!r}"
        )

    return Probe(
        id=probe_id,
        tu=str(raw["tu"]),
        subject=str(raw["subject"]),
        action=str(raw["action"]),
        timeout_s=ts,
        requires=list(raw.get("requires") or []),
        downstream_assertion=dict(raw["downstream_assertion"]),
        encoding_assertion=bool(raw.get("encoding_assertion", False)),
    )


_FENCED_PROBE_BLOCK_RE = re.compile(
    r"### integration_probes\s*\n```(?:yaml)?\s*\n(.*?)\n```",
    re.DOTALL,
)


def parse_probes_from_markdown(markdown_text: str) -> List[Probe]:
    """Extract probes from `### integration_probes` fenced YAML block.

    Returns [] if no such block exists. Raises ProbeSchemaError on any
    malformed probe within the block.
    """
    m = _FENCED_PROBE_BLOCK_RE.search(markdown_text)
    if not m:
        return []
    yaml_text = m.group(1)
    try:
        import yaml  # lazy import — pyyaml is installed
    except ImportError:
        raise ProbeSchemaError(
            "pyyaml not installed; required to parse integration_probes block"
        )
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise ProbeSchemaError(f"invalid YAML in integration_probes: {e}")
    if data is None:
        return []
    if not isinstance(data, list):
        raise ProbeSchemaError(
            f"integration_probes block must be YAML list; got {type(data).__name__}"
        )
    return [validate_probe_dict(d) for d in data]


def parse_probes_from_plan_path(plan_path: Path) -> List[Probe]:
    """Read plan file; extract probes."""
    text = plan_path.read_text(encoding="utf-8")
    return parse_probes_from_markdown(text)
