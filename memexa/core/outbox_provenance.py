"""TU-3 of 2026 backfill plan_v2 §3 — outbox provenance wrapper.

Per plan §TaskUnits TU-3 Action 3: enrich hindsight_outbox.enqueue with
`provenance: dict | None` field for backfill-source attribution without
breaking existing callers.

Strategy: wrap existing enqueue with a side-channel provenance log in
data/outbox_provenance.jsonl (parallel to outbox.jsonl). The wrapper
preserves the existing enqueue contract — backwards-compatible with all
chat-realtime / chat-graph / hindsight callers.

Schema for outbox_provenance.jsonl:
  {
    outbox_id: str (sha256[:32] of file_path|content_sha256),
    provenance: {
      source_kind: str (∈ SOURCE_KIND_ENUM),
      source_offset: str,
      extracted_by: str (∈ EXTRACTED_BY_BACKFILL),
      extraction_prompt_sha: str (sha256[:32]),
      ingest_ts: float (epoch),
      task_id: str (autopilot task_id at ingest time),
    }
  }

axis_anchor: [C:hook:outbox_provenance]
trace event: outbox_provenance_enqueued / outbox_provenance_skipped
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from memexa.core.fact_schema import (
    HASH_LEN,
    SOURCE_KIND_ENUM,
    EXTRACTED_BY_BACKFILL,
)
from memexa.core.hindsight_outbox import enqueue as _core_enqueue


DEFAULT_PROVENANCE_PATH = Path("data/outbox_provenance.jsonl")


def compute_outbox_id(file_path: str, content_sha256: str) -> str:
    """sha256[:HASH_LEN] of file_path|content_sha256 — matches outbox._outbox_id."""
    raw = f"{file_path}|{content_sha256}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:HASH_LEN]


def validate_provenance(provenance: Dict[str, Any]) -> tuple[bool, str]:
    """G1-style schema check on provenance dict.

    Required fields: source_kind, source_offset, extracted_by,
    extraction_prompt_sha, ingest_ts, task_id.
    """
    if not isinstance(provenance, dict):
        return False, "provenance_not_dict"

    required = {
        "source_kind", "source_offset", "extracted_by",
        "extraction_prompt_sha", "ingest_ts", "task_id",
    }
    actual = set(provenance.keys())
    missing = required - actual
    if missing:
        return False, f"provenance_missing_fields:{','.join(sorted(missing))}"

    if provenance["source_kind"] not in SOURCE_KIND_ENUM:
        return False, f"provenance_source_kind_unknown:{provenance['source_kind']!r}"

    if provenance["extracted_by"] not in EXTRACTED_BY_BACKFILL:
        return False, f"provenance_extracted_by_not_backfill:{provenance['extracted_by']!r}"

    if not isinstance(provenance["source_offset"], str) or not provenance["source_offset"]:
        return False, "provenance_source_offset_empty"

    sha = provenance["extraction_prompt_sha"]
    if not isinstance(sha, str) or len(sha) != HASH_LEN:
        return False, "provenance_extraction_prompt_sha_invalid"

    ts = provenance["ingest_ts"]
    if not isinstance(ts, (int, float)) or ts <= 0:
        return False, "provenance_ingest_ts_invalid"

    if not isinstance(provenance["task_id"], str) or not provenance["task_id"]:
        return False, "provenance_task_id_empty"

    return True, ""


def enqueue_with_provenance(
    file_path: str,
    content_sha256: str,
    content_bytes: bytes,
    provenance: Dict[str, Any],
    dir: Optional[str] = None,
    provenance_path: Optional[Path] = None,
) -> tuple[bool, str]:
    """Enqueue + attach backfill provenance side-log.

    Returns (ok, reason). On success, returns (True, ""); on failure:
    - (False, "provenance_invalid:<reason>") if provenance fails validation
    - (False, "core_enqueue_failed") if hindsight_outbox.enqueue returns False

    Two-phase write:
      1. Validate provenance schema
      2. Call core enqueue (which uses FileLock + 10MB refuse)
      3. On enqueue success, append to provenance side-log
      4. If side-log fails, the core entry remains (best-effort fail);
         provenance is recoverable from the source via re-extraction.
    """
    # Phase 1: schema validation
    ok, reason = validate_provenance(provenance)
    if not ok:
        return False, f"provenance_invalid:{reason}"

    # Phase 2: core enqueue
    enqueue_ok = _core_enqueue(file_path, content_sha256, content_bytes, dir=dir)
    if not enqueue_ok:
        return False, "core_enqueue_failed"

    # Phase 3: side-log
    obx_id = compute_outbox_id(file_path, content_sha256)
    entry = {
        "outbox_id": obx_id,
        "provenance": dict(provenance),
        "logged_at": time.time(),
    }
    path = provenance_path or DEFAULT_PROVENANCE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        # Per logic-iter1-6 fix: degraded log is a partial failure → return False
        # so caller knows provenance audit trail is missing. Core enqueue is
        # already committed (idempotent), so a retry won't duplicate.
        return False, f"provenance_log_degraded:{e}"

    return True, ""


def lookup_provenance(
    outbox_id: str,
    provenance_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Reverse lookup: outbox_id → provenance dict (or None if missing)."""
    path = provenance_path or DEFAULT_PROVENANCE_PATH
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("outbox_id") == outbox_id:
                return entry.get("provenance")
    return None


__all__ = [
    "DEFAULT_PROVENANCE_PATH",
    "compute_outbox_id",
    "validate_provenance",
    "enqueue_with_provenance",
    "lookup_provenance",
]
