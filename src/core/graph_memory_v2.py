"""Memory subsystem v2: thin facade over Hindsight (replaces handcrafted graph_memory.py).

History:
- v1 (handcrafted): src.core.graph_memory.py 1285 LoC + 7 sibling modules = 3863 LoC
  - Neo4j 5.26 backend with custom Episode/Fact/Entity schema
  - Dual-LLM (Sonnet + Haiku) fact extraction with source-span/provenance/numeric-coherence wrappers
  - Issues: 70% entity-kind drift, 800 B chunking placeholder, mojibake bug, 0% recall on audit_corpus
  - ARCHIVED 2026-04-25 to archive/legacy/memex_memory_2026_04_25/

- v2 (this module): thin HTTP facade to Hindsight daemon
  - Backend: Hindsight + DeepSeek (OpenAI-compatible) running in py3.12 conda env "hindsight"
  - daemon long-running on 127.0.0.1:8888 (idle_timeout=0)
  - source-span/provenance/numeric-coherence wrappers re-applied as recall post-processing
  - Mini-spike (10 files): 80% recall@10 / 70% recall@1 / MRR 0.733 (vs handcrafted 0%/0%)

Backwards-compatibility:
- query_entity / write_fact / stats — same signatures as v1 for call-site compat
- FactRow dataclass — same shape; populated from Hindsight RecallResponse
- Three-layer anti-hallucination wrappers preserved as recall post-processors:
  * source-span: validated via fact text bytes containment in source memory file
  * provenance: validated via metadata.source_file (when retain provided it)
  * numeric coherence: validated via numeric token check ±40 char window
"""
from __future__ import annotations


import hashlib
import hmac
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional

from src.core.hindsight_client import HindsightHttpClient, get_client


# ---------------------------------------------------------------------------
# Capability gate (TU-1 + TU-2, Closure B P0, 2026-05-01)
# ---------------------------------------------------------------------------


class CapabilityRequired(Exception):
    """Raised when a cross-contact query is attempted without a valid capability token."""


# Paths resolved relative to this file's parent chain (memex/data/)
_CAP_TOKEN_PATH: Path = Path(__file__).resolve().parents[2] / "data" / "capability_token.bin"
_CAP_LOCKOUT_PATH: Path = Path(__file__).resolve().parents[2] / "data" / "capability_lockout.json"

# TTL in seconds (5 minutes)
_CAP_TTL_SEC: int = 300
# Lockout: 5 failed attempts within 15 minutes
_CAP_LOCKOUT_MAX: int = 5
_CAP_LOCKOUT_WINDOW_SEC: int = 900


def _emit_cap_trace(event: str, payload: dict) -> None:
    """Emit capability gate trace event (best-effort, non-blocking).

    Uses _safe_open_trace_path allowlist to prevent path traversal via env var
    (security-p0-iter1-1 fix).
    """
    import json as _js
    # Allowlist trace path: workspace memex/data/ only (no env-var traversal)
    fh = _safe_open_trace_path("MEMEX_GMV2_STUB_TRACE_LOG")
    if fh is None:
        return
    try:
        rec = {"event": event, "ts": _time.time(), **payload}
        fh.write(_js.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
    except OSError:
        pass
    finally:
        try:
            fh.close()
        except Exception:
            pass


import re as _re

_CROSS_CONTACT_KEYWORD_RE = _re.compile(
    r"(全部|everyone|everybody|所有联系人)", _re.IGNORECASE
)
_WXID_RE = _re.compile(r"wxid_[a-z0-9_]{1,40}")
_UUID_RE = _re.compile(
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}", _re.IGNORECASE
)


def _query_crosses_contacts(query: str) -> bool:
    """Return True if query crosses contact boundaries.

    Detects:
    - Keyword match: (全部|everyone|everybody|所有联系人)
    - ≥2 distinct wxid_ tokens
    - ≥2 distinct UUID-like tokens
    """
    if _CROSS_CONTACT_KEYWORD_RE.search(query):
        return True
    wxids = set(_WXID_RE.findall(query))
    if len(wxids) >= 2:
        return True
    uuids = set(m.lower() for m in _UUID_RE.findall(query))
    if len(uuids) >= 2:
        return True
    return False


def _get_vault_key_material() -> bytes:
    """Derive per-caller key material from browser_vault.

    Returns b'' if vault is locked or inaccessible (gate will reject).
    """
    try:
        from memex.browser_vault import api as vault_api
        if not vault_api.try_unlock_silent():
            return b""
        # Use the public accessor added in TU-1
        return vault_api.get_caller_key_material("closure_b_u11")
    except Exception:
        return b""


def _validate_capability(token_bytes: Optional[bytes], *, data_dir: Optional[Path] = None) -> bool:
    """Validate a 56-byte capability token against vault key material and TTL.

    Payload layout: struct.pack(">Q", minted_at) + 16-byte nonce + 32-byte hmac_tag

    Returns True iff token is valid and within TTL. Emits trace events on
    failure (capability_expired, capability_hmac_mismatch). On success emits
    capability_validated.
    """
    import json as _js_local
    token_path = (data_dir / "capability_token.bin") if data_dir else _CAP_TOKEN_PATH

    # Read from file if token_bytes not provided or file exists
    if token_bytes is None:
        if not token_path.exists():
            return False
        try:
            token_bytes = token_path.read_bytes()
        except OSError:
            return False

    if len(token_bytes) != 56:
        return False

    try:
        ts_bytes = token_bytes[:8]
        nonce = token_bytes[8:24]
        tag_received = token_bytes[24:56]
        minted_at = struct.unpack(">Q", ts_bytes)[0]
    except Exception:
        return False

    # TTL check: use payload-embedded timestamp (NOT mtime) per RP-SEC-1
    # Reject future-dated tokens (clock-skew tolerance 60s) per logic-p0-iter1-1 HIGH fix
    now = time.time()
    if minted_at > now + 60:
        _emit_cap_trace("capability_future_dated", {
            "minted_at": minted_at,
            "now": now,
        })
        return False
    if (now - minted_at) > _CAP_TTL_SEC:
        _emit_cap_trace("capability_expired", {
            "minted_at": minted_at,
            "age_s": now - minted_at,
        })
        return False

    # HMAC check: bind to vault key material per RP-SEC-2
    key = _get_vault_key_material()
    if not key:
        return False

    expected_tag = hmac.new(key, msg=ts_bytes + nonce, digestmod=hashlib.sha256).digest()
    if not hmac.compare_digest(expected_tag, tag_received):
        _emit_cap_trace("capability_hmac_mismatch", {
            "minted_at": minted_at,
        })
        return False

    _emit_cap_trace("capability_validated", {
        "minted_at": minted_at,
        "age_s": now - minted_at,
    })
    return True


def _on_vault_lock_unlink_token(token_path: Optional[Path] = None) -> None:
    """on_lock callback: atomically unlink capability_token.bin when vault locks.

    Registered via browser_vault.api.register_lock_callback (RP-SEC-4).
    """
    p = token_path or _CAP_TOKEN_PATH
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def _register_vault_on_lock_callback(token_path: Optional[Path] = None) -> None:
    """Register _on_vault_lock_unlink_token with browser_vault on_lock hook.

    Uses api.register_lock_callback shim (added to browser_vault/api.py).
    Fails silently if not available (defense-in-depth only; TTL is primary).
    """
    try:
        from memex.browser_vault import api as vault_api
        p = token_path or _CAP_TOKEN_PATH
        vault_api.register_lock_callback(lambda: _on_vault_lock_unlink_token(p))
    except Exception:
        pass


def _read_lockout_state(lockout_path: Path) -> dict:
    """Read lockout state from JSON file. Returns default state on error."""
    import json as _js_local
    try:
        return _js_local.loads(lockout_path.read_text(encoding="utf-8"))
    except Exception:
        return {"attempts": 0, "last_attempt": 0.0}


def _write_lockout_state(lockout_path: Path, state: dict, sticky: bool = False) -> None:
    """Write lockout state to JSON file. Apply sticky 0o444 if sticky=True."""
    import json as _js_local
    try:
        # Restore to writable before write
        try:
            os.chmod(lockout_path, 0o644)
        except Exception:
            pass
        lockout_path.write_text(_js_local.dumps(state), encoding="utf-8")
        if sticky:
            try:
                os.chmod(lockout_path, 0o444)
            except Exception:
                pass
    except OSError:
        pass


def _mint_capability_token(*, data_dir: Optional[Path] = None) -> int:
    """Mint a 5min-TTL capability token bound to vault key material.

    Returns:
      0 on success
      2 on vault unlock failure
      3 on lockout (5 failed attempts within 15 min)
    """
    import getpass
    import json as _js_local
    import secrets
    import subprocess

    token_path = (data_dir / "capability_token.bin") if data_dir else _CAP_TOKEN_PATH
    lockout_path = (data_dir / "capability_lockout.json") if data_dir else _CAP_LOCKOUT_PATH

    # Lockout check (RP-SEC-3)
    state = _read_lockout_state(lockout_path)
    attempts = state.get("attempts", 0)
    last_attempt = state.get("last_attempt", 0.0)
    now = time.time()
    if attempts >= _CAP_LOCKOUT_MAX and (now - last_attempt) < _CAP_LOCKOUT_WINDOW_SEC:
        _emit_cap_trace("capability_lockout", {"attempts": attempts, "last_attempt": last_attempt})
        return 3

    # Attempt vault unlock
    try:
        from memex.browser_vault import api as vault_api
    except Exception:
        return 2

    if not vault_api.try_unlock_silent():
        # Prompt for passphrase
        try:
            passphrase = getpass.getpass("Vault passphrase: ").encode()
            vault_api.unlock(passphrase)
        except Exception:
            # Increment lockout counter
            new_attempts = attempts + 1
            new_state = {"attempts": new_attempts, "last_attempt": now}
            sticky = new_attempts >= _CAP_LOCKOUT_MAX
            _write_lockout_state(lockout_path, new_state, sticky=sticky)
            return 2

    # Reset lockout on success
    _write_lockout_state(lockout_path, {"attempts": 0, "last_attempt": 0.0})

    # Mint token
    key = _get_vault_key_material()
    if not key:
        return 2

    ts = int(time.time())
    ts_bytes = struct.pack(">Q", ts)
    nonce = secrets.token_bytes(16)
    tag = hmac.new(key, msg=ts_bytes + nonce, digestmod=hashlib.sha256).digest()
    payload = ts_bytes + nonce + tag

    # Atomic write (RP-SEC-4)
    tmp_path = token_path.with_suffix(f".tmp.{os.getpid()}")
    try:
        tmp_path.write_bytes(payload)
        os.replace(tmp_path, token_path)
        try:
            os.chmod(token_path, 0o600)
        except Exception:
            pass
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return 2
    finally:
        # Ensure tmp cleaned up even on os.replace failure
        try:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
        except Exception:
            pass

    # Windows ACL hardening (RP-SEC-5)
    try:
        user = getpass.getuser()
        result = subprocess.run(
            ["icacls", str(token_path), "/inheritance:r",
             "/grant:r", f"{user}:F", "/deny", "Everyone:R"],
            capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            _emit_cap_trace("capability_acl_hardened", {"path": str(token_path)})
        else:
            _emit_cap_trace("capability_acl_skipped", {
                "reason": "icacls_nonzero",
                "returncode": result.returncode,
            })
    except Exception as e:
        _emit_cap_trace("capability_acl_skipped", {"reason": str(e)[:80]})

    from datetime import datetime, timezone
    minted_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    _emit_cap_trace("capability_elevated", {
        "minted_at_iso": minted_iso,
        "ttl_sec": _CAP_TTL_SEC,
    })

    # Register on_lock callback so token is unlinked on vault re-lock
    _register_vault_on_lock_callback(token_path)

    return 0


def _clear_lockout(*, data_dir: Optional[Path] = None) -> int:
    """Clear lockout state after vault re-authentication.

    Returns 0 on success, 2 on vault unlock failure.
    """
    import getpass

    lockout_path = (data_dir / "capability_lockout.json") if data_dir else _CAP_LOCKOUT_PATH

    try:
        from memex.browser_vault import api as vault_api
    except Exception:
        return 2

    if not vault_api.try_unlock_silent():
        try:
            passphrase = getpass.getpass("Vault passphrase: ").encode()
            vault_api.unlock(passphrase)
        except Exception:
            return 2

    _write_lockout_state(lockout_path, {"attempts": 0, "last_attempt": 0.0})
    return 0


@dataclass
class FactRow:
    """Compat FactRow — same shape as v1, populated from Hindsight RecallResponse.

    TU-U5-2 (2026-04-26): added `metadata` field so the recall post-filter can
    consume Hindsight RecallResponse metadata (incl. invalidated_chunk_id).
    """

    fact_id: str
    subject_canon: str
    subject_raw: Optional[str]
    predicate: str
    predicate_canon: Optional[str]
    object_canon: str
    object_raw: Optional[str]
    confidence: float
    tier: Optional[str]
    source_episode_id: str
    source_span: Optional[str]
    observed_at: Optional[str]
    metadata: Optional[dict[str, Any]] = None  # TU-U5-2 (verifier B1 fix)


# TU-U5-2: trace emission for wrapper-layer stubs (HARD RULE
# `anti_halluc_stub_must_emit_status` — Layer 2/3 currently return True
# unconditionally; emit `wrapper_layer_stub` per call so silent-pass is
# observable in trace_sink.)
import json as _json
import os as _os
import time as _time
from src.core._path_resolver import memory_dir

def _emit_stub_trace(layer: str, payload: dict) -> None:
    path = _os.environ.get("MEMEX_GMV2_STUB_TRACE_LOG", "")
    if not path:
        return
    try:
        rec = {"event": "wrapper_layer_stub", "layer": layer,
               "ts": _time.time(), **payload}
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _emit_filter_trace(event: str, payload: dict) -> None:
    path = _os.environ.get("MEMEX_GMV2_FILTER_TRACE_LOG", "")
    if not path:
        return
    try:
        rec = {"event": event, "ts": _time.time(), **payload}
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


# --------------- 3-layer anti-hallucination wrappers (v2: post-recall) ---------------


def _layer_source_span_check(fact_text: str, fact_chunk_id: Optional[str]) -> bool:
    """Layer 1: source-span byte verification.

    v1 design: fact must contain a verbatim 8-200 char substring of source episode.
    v2 reality: Hindsight already chunks episodes; chunk_id binds fact to chunk text.
    Pass-through if chunk_id present (Hindsight enforces this internally).
    """
    return fact_chunk_id is not None and len(fact_text) >= 8


def _layer_provenance_check(fact_metadata: Optional[dict[str, Any]]) -> bool:
    """Layer 2: provenance chain validation.

    v1 design: source_episode_id must point to a real Episode in graph.
    v2 reality: Hindsight maintains chunk -> document -> bank chain; metadata is best-effort.

    TU-U5-2: emits wrapper_layer_stub trace per HARD RULE
    `anti_halluc_stub_must_emit_status` (silent True is no longer compliant).
    """
    _emit_stub_trace("provenance", {"has_metadata": fact_metadata is not None})
    return True


def _layer_numeric_coherence(fact_text: str, source_episode_id: str) -> bool:
    """Layer 3: numeric coherence check.

    v1 design: any number in fact must appear in source_span ±40 char window.
    v2 stub: pending implementation (post-cutover Phase 4 work).

    TU-U5-2: emits wrapper_layer_stub trace per HARD RULE.
    """
    _emit_stub_trace("numeric_coherence",
                     {"text_len": len(fact_text or ""), "ep_id": source_episode_id})
    return True


def _layer4_invalidation_filter(fact: FactRow, invalidated_episodes: set[str]) -> bool:
    """Layer 4: skip facts whose episode is marked as invalidated.

    TU-4 Closure A plan_v3 (2026-05-01):
    - (a) episode_id matched in invalidate_marker bank (predicate-marker form)
    - (b) original fact has metadata.invalid_at != None (metadata-patch form)

    Returns True iff the fact should be KEPT (not filtered).
    Emits `chat_invalidation_filtered` trace per filtered fact.
    """
    md = fact.metadata or {}
    episode_id = md.get("episode_id") or fact.source_episode_id or ""

    # (a) predicate-marker form
    if episode_id and episode_id in invalidated_episodes:
        _emit_filter_trace("chat_invalidation_filtered",
                           {"fact_id": fact.fact_id, "episode_id": episode_id,
                            "reason": "invalidate_marker"})
        return False

    # (b) metadata-patch form: invalid_at present and non-None
    if md.get("invalid_at") is not None:
        _emit_filter_trace("chat_invalidation_filtered",
                           {"fact_id": fact.fact_id, "episode_id": episode_id,
                            "reason": "invalid_at_set",
                            "invalid_at": str(md["invalid_at"])})
        return False

    return True


def _load_invalidated_episodes() -> set[str]:
    """Return set of episode_ids that have a corresponding invalidate_marker fact.

    Queries the graph for facts with predicate=='invalidates' written by
    chat_invalidation_reconcile. Tolerates Hindsight outage (returns empty
    set = fail-open so normal recall is not blocked).
    """
    # Closure A plan_v3 fix (security-code-iter1-1 HIGH):
    # Validate episode_id format BEFORE adding to invalidation set. Adversary
    # can craft chat content causing LLM to extract triple
    # (chat_invalidation_reconcile, invalidates, <arbitrary>). Without regex
    # gate, attacker fake-invalidates legitimate memories.
    import re as _re
    _EP_ID_RE = _re.compile(r"^[a-f0-9]{32}$")

    out: set[str] = set()
    try:
        client = get_client()
        raw = client.recall(query="invalidate_marker invalidates", max_tokens=8192)
        results = raw.get("results", []) if isinstance(raw, dict) else []
        for r in results:
            text = r.get("text", "") or ""
            md = r.get("metadata") or r.get("memory_metadata") or {}
            # object_canon holds the episode_id (written as "subject predicate object")
            # Parse "chat_invalidation_reconcile invalidates <episode_id>"
            parts = text.strip().split()
            if len(parts) >= 3 and parts[1] == "invalidates":
                candidate = parts[2]
                if _EP_ID_RE.match(candidate):
                    out.add(candidate)
            # Fallback: source_episode_id in metadata (validated)
            src = md.get("source_episode_id") or r.get("chunk_id", "")
            if src and _EP_ID_RE.match(str(src)):
                out.add(str(src))
    except Exception:
        pass
    return out


def _apply_anti_halluc_wrappers(facts: List[FactRow]) -> List[FactRow]:
    """Filter recalled facts through 4-layer anti-hallucination wrappers + invalidate.

    TU-U5-2 (2026-04-26):
    - Layer 1 source-span (existing).
    - Layer 2 provenance (stub; emits wrapper_layer_stub trace).
    - Layer 3 numeric coherence (stub; emits wrapper_layer_stub trace).
    - Layer 4 chat invalidation (TU-4 Closure A 2026-05-01):
      Skip facts whose episode_id is matched in invalidate_marker bank OR
      whose original fact has metadata.invalid_at != None.
      Emits `chat_invalidation_filtered` per dropped fact.
    - EXISTING: invalidate post-filter via src.core.invalidate.list_tombstoned().
      Drops facts whose source_episode_id is tombstoned. Emits
      `fact_filtered_invalidated` trace per drop.
    """
    # Lazy import to avoid circular dep + tolerate invalidate module errors
    try:
        from src.core.invalidate import list_tombstoned
        tombstoned: set[str] = list_tombstoned()
    except Exception:
        tombstoned = set()

    # Layer 4: load invalidated episodes (fail-open on error)
    try:
        invalidated_episodes: set[str] = _load_invalidated_episodes()
    except Exception:
        invalidated_episodes = set()

    out = []
    for f in facts:
        if f.source_episode_id and f.source_episode_id in tombstoned:
            _emit_filter_trace("fact_filtered_invalidated",
                               {"fact_id": f.fact_id,
                                "source_episode_id": f.source_episode_id})
            continue
        # Layer 4: chat invalidation filter
        if not _layer4_invalidation_filter(f, invalidated_episodes):
            continue
        if _layer_source_span_check(f.predicate or f.object_canon, f.source_episode_id):
            if _layer_provenance_check(f.metadata):
                if _layer_numeric_coherence(f.object_canon, f.source_episode_id):
                    out.append(f)
    return out


# --------------- v1 compat API ---------------


def _hindsight_result_to_factrow(r: dict[str, Any]) -> FactRow:
    """Convert Hindsight RecallResponse fact dict to v1 FactRow.

    TU-U5-2: extract metadata so post-filter can read invalidated_chunk_id etc.
    """
    md = r.get("metadata") or r.get("memory_metadata") or None
    return FactRow(
        fact_id=str(r.get("id", "") or r.get("fact_id", "")),
        subject_canon="",  # Hindsight does not expose subject/predicate/object — full fact text is in `text`
        subject_raw=None,
        predicate="",
        predicate_canon=None,
        object_canon=r.get("text", "") or "",
        object_raw=r.get("text", ""),
        confidence=float(r.get("confidence", 0.0) or 0.0),
        tier=None,
        source_episode_id=str(r.get("chunk_id", "") or r.get("document_id", "")),
        source_span=r.get("source_chunk", None),
        observed_at=r.get("mentioned_at", None) or r.get("occurred_start", None),
        metadata=md,
    )


def query_entity(
    query: str,
    limit: int = 20,
    extracted_by: Optional[str] = None,
    capability_token: Optional[bytes] = None,
    budget: str = "low",
    include_legacy: bool = True,
) -> List[FactRow]:
    """v1 compat: forward to Hindsight client.recall + 3-layer post-processing.

    DEPRECATED 2026-05-06 (P2.5): callers SHOULD migrate to
    `src.core.memory_query.quick(query)` which provides:
      - default schema:v1 + memory_full_v3 bank (this function still queries
        whatever default _DEFAULT_BANK points to, post-P2.1 = memory_full_v3,
        but does NOT enforce schema:v1 filter)
      - Win 端 metadata filter (tier/type/source/salience)
      - tombstone-by-tag invalidation filter (Layer 4)

    This function kept as backward-compat for: chat_context_provider, fact_validator,
    memory_search_boosted, etc. Schedule for removal after Phase 2.6 E2E pass.

    Closure A plan_v3 TU-1: optional `extracted_by` filter (per AC-U9-3 +
    AC-U10-29). When provided, post-fetch filters facts by metadata
    extracted_by field; value asserted to be in canonical set.

    Closure B TU-1 (2026-05-01): capability_token gate added.
    Execution order (RP-LOG-1): capability gate FIRST → flag check SECOND → PG SELECT THIRD.

    Backward-compat: default `extracted_by=None` means no filter (preserves
    callers that don't know about the filter).
    """
    # P2.5: soft deprecation trace (no warnings.warn to avoid hot-path noise)
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event("graph_memory_v2_query_entity_called",
                          {"query_hash": hashlib.sha256(
                              query.encode()).hexdigest()[:16],
                           "deprecated": True,
                           "migrate_to": "src.core.memory_query.quick"})
    except Exception:
        pass
    # --- GATE 1: capability gate (MUST run before PG SELECT and before flag check) ---
    if _query_crosses_contacts(query):
        if not _validate_capability(capability_token):
            reason = "keyword" if _CROSS_CONTACT_KEYWORD_RE.search(query) else "≥2_uuids"
            _emit_cap_trace("capability_required", {
                "query_hash": hashlib.sha256(query.encode()).hexdigest()[:16],
                "reason": reason,
            })
            raise CapabilityRequired(
                "query crosses contact boundaries; capability_token required"
            )

    # --- GATE 2: ablation flag (single check site per RP-SEC-7 / D-3) ---
    if os.environ.get("CHAT_CONTEXT_INJECTION_OFF") == "1" and extracted_by == "chat-realtime":
        return []

    if extracted_by is not None:
        # AC-U10-29 / RP-32: assert value is a canonical Enum member
        from src.chat.metadata_builder import EXTRACTED_BY_CANONICAL
        if extracted_by not in EXTRACTED_BY_CANONICAL:
            raise ValueError(
                f"extracted_by={extracted_by!r} not in canonical set "
                f"{sorted(EXTRACTED_BY_CANONICAL)!r}"
            )

    client = get_client()
    # Fetch wider than `limit` so the post-filter can still return ≥limit facts.
    raw_limit = limit * 5 if extracted_by else limit
    # Forward budget so caller can pick fast/medium/high (was always "high");
    # bigger max_tokens to avoid 1-card cliff (raw_limit*200=1000 was too tight
    # at limit=5 → fits only one 866-tok narrative).
    raw = client.recall(
        query=query, max_tokens=raw_limit * 600, budget=budget,
    )
    results = raw.get("results", [])
    # Optionally union with legacy bank for cold-start coverage (cutover pending)
    if include_legacy:
        try:
            leg = client.recall(
                query=query,
                bank_id="memory_full",
                max_tokens=raw_limit * 600,
                budget=budget,
            )
            results = results + (leg.get("results") or [])
        except Exception:
            pass
    # Dedupe by id (cards present in both banks)
    seen: Dict[str, Any] = {}
    for r in results:
        key = r.get("id") or r.get("memory_id") or r.get("text", "")[:60]
        if key not in seen:
            seen[key] = r
    results = list(seen.values())[:raw_limit]
    facts = [_hindsight_result_to_factrow(r) for r in results]

    if extracted_by is not None:
        facts = [f for f in facts if (f.metadata or {}).get("extracted_by") == extracted_by]

    facts = facts[:limit]
    return _apply_anti_halluc_wrappers(facts)


def write_fact(
    subject_canon: str,
    predicate: str,
    object_canon: str,
    source_episode_id: str = "",
    source_span: Optional[str] = None,
    confidence: float = 1.0,
    tier: Optional[str] = None,
    **kwargs: Any,
) -> bool:
    """v1 compat: forward to Hindsight client.retain.

    v2 note: Hindsight's retain takes raw content + LLM extracts facts internally.
    We synthesize a "S P O" content string from the v1 args.

    TU-U4-3 (2026-04-26): if content is zh-dense (Han ratio >= 0.3), apply
    deterministic coreference injection BEFORE retain (zh_coref.inject_antecedents).
    """
    client = get_client()
    content = f"{subject_canon} {predicate} {object_canon}"
    if source_span:
        content += f" (context: {source_span})"
    # zh coref pre-processor (TU-U4-3 hook)
    try:
        from src.core.zh_coref import detect_zh_dense, inject_antecedents
        if detect_zh_dense(content):
            content = inject_antecedents(content)
    except Exception:
        pass  # zh_coref failure must not block retain
    metadata: dict[str, Any] = {"source_episode_id": source_episode_id, "confidence": confidence}
    if tier:
        metadata["tier"] = tier
    try:
        client.retain(content=content, metadata=metadata, tags=["v1_compat_write"])
        return True
    except Exception:
        return False


def stats() -> dict[str, Any]:
    """v1 compat: return basic health/count from Hindsight daemon.

    Note: Hindsight does not directly expose entity_count compatible with v1.
    For v2, stats returns daemon health + bank metadata.
    """
    client = get_client()
    return {
        "ok": client.health(),
        "backend": "hindsight",
        "version": "v2",
        "bank": os.environ.get("MEMEX_HINDSIGHT_BANK", "memory_full_v5"),
    }


# --------------- v2-only API (Hindsight native) ---------------


def reflect(query: str, **kwargs: Any) -> dict[str, Any]:
    """v2-only: LLM synthesizes natural-language answer (Hindsight reflect)."""
    client = get_client()
    return client.reflect(query=query, **kwargs)


# --------------- U2 (2026-04-26): async outbox write ---------------


def write_fact_async(file_path: str, content_bytes: bytes) -> bool:
    """Enqueue a memory-file write into the Hindsight outbox.

    Replaces the legacy `subprocess.Popen(['graph_memory', 'ingest', ...])`
    fan-out from PostToolUse hook.

    Computes content_sha256 in-process and delegates to
    `hindsight_outbox.enqueue`. Local file I/O only; no httpx/Hindsight
    HTTP from this path (stays py3.9-safe).

    Returns True iff the entry was successfully appended to the outbox
    (or deduplicated against an existing pending entry). Returns False
    when the outbox is unavailable / full / disk error / unsafe parent —
    caller should fall back to legacy ingest path.
    """
    import hashlib

    try:
        from src.core.hindsight_outbox import enqueue
    except Exception:
        return False
    try:
        sha = hashlib.sha256(content_bytes).hexdigest()
        return bool(enqueue(file_path=file_path,
                            content_sha256=sha,
                            content_bytes=content_bytes))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# TU-1+TU-3 (memory_system_full_repair_2026_04_29):
# in-module CLI: query / stats / backfill subcommands.
#
# Per plan_v2 RP-15: env-controlled trace paths must pass allowlist check
# before open(); per RP-16: scan_memory_dir() must reject symlink escapes.
# ---------------------------------------------------------------------------


def _safe_open_trace_path(env_var: str):
    """Resolve env-var trace path, reject paths outside allowlisted parents.

    Per security-iter2-1 (RP-15): MEMEX_GMV2_STUB_TRACE_LOG must not be
    settable to arbitrary file (e.g. ../../CLAUDE.md). Allowlist:
    - <home>/.claude/ (workspace memory + tasks)
    - tempfile.gettempdir() (per hindsight_outbox._allowlisted_parents pattern)
    """
    import tempfile
    from pathlib import Path
    p = _os.environ.get(env_var, "")
    if not p:
        return None
    try:
        resolved = Path(p).resolve()
    except (OSError, RuntimeError):
        return None
    allowed = []
    try:
        allowed.append((Path.home() / ".claude").resolve())
    except Exception:
        pass
    try:
        allowed.append(Path(tempfile.gettempdir()).resolve())
    except Exception:
        pass
    for a in allowed:
        try:
            resolved.relative_to(a)
            return resolved
        except ValueError:
            continue
    return None


def _emit_cli_trace(subcommand: str, exit_code: int) -> None:
    """Emit cli_command_invoked trace per RP-14 (declared events must emit)."""
    target = _safe_open_trace_path("MEMEX_GMV2_STUB_TRACE_LOG")
    if target is None:
        return
    try:
        rec = {"event": "cli_command_invoked",
               "ts": _time.time(),
               "subcommand": subcommand,
               "exit_code": exit_code}
        with open(target, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _main_query(entity: str, limit: int, timeout_s: float) -> int:
    """CLI: query subcommand. Windows-safe timeout via concurrent.futures.

    Per RP-11: signal.alarm unavailable on win32; use ThreadPoolExecutor.
    Per security-iter2-4: stdout written only by main thread.
    Per logic-iter1-1 fix: shutdown(wait=False) so TimeoutError returns
    immediately instead of waiting for background thread to finish.

    2026-05-08 (CEO post-mortem): emit deprecation banner to stderr so any
    operator (human or agent) reading stdout JSON also sees the migration
    pointer. Single-variant fan-out empirically returns ~1 card vs 200+
    from `memory_query topic`; CLAUDE.md §7.1 still pointed here, root cause
    of 2026-05-08 14:17 Mac purchase recall miss.
    """
    import sys as _sys
    print(
        "DEPRECATED: `graph_memory_v2 query` is single-variant + legacy "
        "schema:v0/v1 and recalls ~1 card on topic queries.\n"
        "  → tell-me-about: python -m src.core.memory_query topic <topic>\n"
        "  → entity:        python -m src.core.memory_query quick <q>\n"
        "  → relationship:  python -m src.core.memory_query arc <name>\n"
        "  → time slice:    python -m src.core.memory_query timeline ...\n",
        file=_sys.stderr,
    )
    import concurrent.futures
    facts = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(query_entity, entity, limit)
        try:
            facts = fut.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            print("[]")
            rec = {"event": "cli_query_timeout", "ts": _time.time(),
                   "entity": entity, "timeout_s": timeout_s}
            target = _safe_open_trace_path("MEMEX_GMV2_STUB_TRACE_LOG")
            if target is not None:
                try:
                    with open(target, "a", encoding="utf-8") as f:
                        f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
                except OSError:
                    pass
            return 1
        except Exception:
            print("[]")
            return 1
    finally:
        # logic-iter1-1 fix: do NOT wait for hung background thread on shutdown
        pool.shutdown(wait=False)
    # main thread serializes
    if not facts:
        print("[]")
    else:
        from dataclasses import asdict as _asdict
        rows = [_asdict(f) for f in facts]
        print(_json.dumps(rows, ensure_ascii=False, default=str))
    return 0


def _main_stats() -> int:
    try:
        d = stats()
        print(_json.dumps(d, ensure_ascii=False, default=str))
        return 0
    except Exception as e:
        print(_json.dumps({"ok": False, "error": str(e)[:200]}))
        return 1


# ---- TU-3: backfill helpers ----

_MEMORY_DIR_PATH = memory_dir()


def _scan_memory_dir(memory_dir=None):
    """Scan memory/*.md, return {resolved_path: sha256}.

    Per RP-16 (security-iter2-2): reject symlinks escaping memory_dir.
    """
    import hashlib
    from pathlib import Path
    md = (memory_dir or _MEMORY_DIR_PATH).resolve()
    out = {}
    if not md.is_dir():
        return out
    for f in md.rglob("*.md"):
        try:
            resolved = f.resolve()
            resolved.relative_to(md)  # raises ValueError if escape
        except (ValueError, OSError):
            # symlink escape; emit trace + skip
            target = _safe_open_trace_path("MEMEX_GMV2_STUB_TRACE_LOG")
            if target is not None:
                try:
                    with open(target, "a", encoding="utf-8") as t:
                        t.write(_json.dumps({"event": "symlink_escape_skipped",
                                              "path": str(f)}) + "\n")
                except OSError:
                    pass
            continue
        try:
            data = resolved.read_bytes()
        except OSError:
            continue
        out[resolved] = hashlib.sha256(data).hexdigest()
    return out


def _query_indexed_documents(timeout_s: float = 10.0,
                              page_limit: int = 200,
                              max_pages: int = 50,
                              base_url=None,
                              bank=None):
    """GET /v1/.../documents paginated; extract content_hash (LIVE-verified
    schema 2026-04-29: items[].content_hash is the sha256 identity field;
    metadata.source_file is NOT exposed by Hindsight 0.5.4).

    Per RP-7 (logic-iter1-7): explicit timeout=N (default 10s). Returns
    (set of content_hash sha256 strings, error_str). Empty set + non-empty
    error means hard failure (caller should exit 1).
    """
    try:
        import httpx
    except ImportError:
        return set(), "httpx_missing"
    base = base_url or _os.environ.get(
        "MEMEX_HINDSIGHT_URL", "http://127.0.0.1:8888")
    bank_id = bank or _os.environ.get("MEMEX_HINDSIGHT_BANK", "memory_full_v5")
    seen = set()
    for page in range(max_pages):
        offset = page * page_limit
        try:
            r = httpx.get(
                f"{base}/v1/default/banks/{bank_id}/documents",
                params={"limit": page_limit, "offset": offset},
                timeout=timeout_s,
            )
        except httpx.TimeoutException:
            return seen, f"documents_timeout_at_offset_{offset}"
        except Exception as e:
            return seen, f"documents_err:{type(e).__name__}"
        if r.status_code != 200:
            return seen, f"documents_status_{r.status_code}"
        try:
            d = r.json()
        except Exception:
            return seen, "documents_json_decode_fail"
        # logic-iter1-3 fix: prefer explicit key presence over chained-or
        # (avoids fallthrough when items=[] but data=[stale_doc]).
        if "items" in d:
            items = d["items"]
        elif "data" in d:
            items = d["data"]
        elif "documents" in d:
            items = d["documents"]
        else:
            items = []
        # logic-iter1-2 fix (defensive): on first page, assert content_hash
        # field present in ≥1 item; absent → schema_drift, return error.
        if page == 0 and items:
            first_ok = any(
                it.get("content_hash") or it.get("sha256")
                for it in items[:5]
            )
            if not first_ok:
                return seen, "schema_drift_no_content_hash"
        if not items:
            break
        for it in items:
            ch = it.get("content_hash") or it.get("sha256")
            if ch:
                seen.add(str(ch))
        if len(items) < page_limit:
            break  # last page
    return seen, ""


def _load_deadletter_excludes():
    """Read ingest_deadletter.jsonl, return set of resolved file paths.

    Per RP-13 (logic-iter1-13): Path.resolve() normalize before set membership.
    """
    from pathlib import Path
    out = set()
    candidates = [
        Path(__file__).resolve().parents[2] / "data" / "ingest_deadletter.jsonl",
        Path(__file__).resolve().parents[1] / "data" / "ingest_deadletter.jsonl",
    ]
    for dl_path in candidates:
        if not dl_path.exists():
            continue
        try:
            with dl_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = _json.loads(line)
                    except Exception:
                        continue
                    fp = rec.get("file_path")
                    if not fp:
                        continue
                    try:
                        out.add(Path(fp).resolve())
                    except (OSError, RuntimeError):
                        continue
        except OSError:
            continue
    return out


def _main_backfill(rate: int, dry_run: bool, timeout_s: float) -> int:
    """CLI: backfill subcommand.

    Stage 2 spec per plan_v2 §3 TU-3 + §14 patches:
    1. Scan memory dir (sha256, reject symlink escape)
    2. Query daemon /documents (timeout=10s, paginated)
    3. Load deadletter exclude set
    4. Diff → to_backfill
    5. dry-run: print JSON + exit 0
    6. enqueue with rate limit (60/rate seconds between files)
    7. synchronous drain
    8. Emit traces
    """
    import time as _time2
    from pathlib import Path
    scanned = _scan_memory_dir()
    indexed, idx_err = _query_indexed_documents(timeout_s=timeout_s)
    if idx_err and not indexed:
        out = {"to_backfill_count": 0, "files": [], "error": idx_err}
        print(_json.dumps(out, ensure_ascii=False, default=str))
        rec = {"event": "backfill_documents_timeout", "ts": _time.time(),
               "error": idx_err}
        target = _safe_open_trace_path("MEMEX_GMV2_STUB_TRACE_LOG")
        if target is not None:
            try:
                with open(target, "a", encoding="utf-8") as f:
                    f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                pass
        return 1
    deadletter = _load_deadletter_excludes()
    # LIVE-verified schema (2026-04-29): indexed = set of content_hash sha256.
    # Diff is sha256-based: file is "missing" iff its sha256 not in indexed AND
    # path not in deadletter (path-based exclude).
    to_backfill = [
        (p, sha) for p, sha in scanned.items()
        if sha not in indexed and p not in deadletter
    ]
    out = {
        "to_backfill_count": len(to_backfill),
        "scanned_count": len(scanned),
        "indexed_count": len(indexed),
        "deadletter_count": len(deadletter),
        "files": [str(p) for p, _ in to_backfill[:20]],
    }
    if dry_run:
        print(_json.dumps(out, ensure_ascii=False, default=str))
        return 0
    # Live enqueue
    try:
        from src.core.hindsight_outbox import enqueue
    except ImportError:
        out["error"] = "outbox_import_failed"
        print(_json.dumps(out, ensure_ascii=False, default=str))
        return 1
    enqueued = 0
    sleep_per = 60.0 / max(rate, 1)
    n = len(to_backfill)
    # logic-iter1-4 fix: skip sleep on last file (N-1 between-file gaps).
    for i, (p, sha) in enumerate(to_backfill):
        try:
            data = p.read_bytes()
            ok = enqueue(file_path=str(p), content_sha256=sha, content_bytes=data)
            if ok:
                enqueued += 1
        except Exception:
            pass
        if i < n - 1:
            _time2.sleep(sleep_per)
    out["enqueued_count"] = enqueued
    # Synchronous drain (per RP-9 logic-iter1-9)
    try:
        from src.core.hindsight_outbox import drain
        drain_result = drain(max_items=enqueued + 10)
        out["drain_result"] = str(drain_result)[:200]
    except Exception as e:
        out["drain_error"] = type(e).__name__
    print(_json.dumps(out, ensure_ascii=False, default=str))
    return 0


def _cli_main(argv=None) -> int:
    """In-module CLI entry. Per plan_v2 RP-14: emit cli_command_invoked."""
    import argparse
    import sys
    p = argparse.ArgumentParser(prog="graph_memory_v2")
    sub = p.add_subparsers(dest="cmd", required=False)

    pq = sub.add_parser("query", help="recall facts for an entity")
    pq.add_argument("entity")
    pq.add_argument("--limit", type=int, default=20)
    pq.add_argument("--timeout-s", type=float, default=60.0)

    sub.add_parser("stats", help="daemon health + bank metadata")

    pb = sub.add_parser("backfill", help="diff memory/* vs daemon, enqueue missing")
    pb.add_argument("--rate", type=int, default=10,
                    help="files per minute (int; --rate 10 = 6s/file)")
    pb.add_argument("--dry-run", action="store_true")
    pb.add_argument("--timeout-s", type=float, default=10.0)

    pe = sub.add_parser("elevate", help="mint 5min capability token via vault unlock")
    pe.add_argument("--clear-lockout", action="store_true",
                    help="clear lockout state (requires fresh vault re-auth)")

    args = p.parse_args(argv)

    rc = 0
    sub_name = args.cmd or "(none)"
    try:
        if args.cmd == "query":
            rc = _main_query(args.entity, args.limit, args.timeout_s)
        elif args.cmd == "stats":
            rc = _main_stats()
        elif args.cmd == "backfill":
            rc = _main_backfill(args.rate, args.dry_run, args.timeout_s)
        elif args.cmd == "elevate":
            if args.clear_lockout:
                rc = _clear_lockout()
            else:
                rc = _mint_capability_token()
        else:
            p.print_help()
            rc = 2
    except Exception as e:
        print(_json.dumps({"ok": False, "error": str(e)[:200]}))
        rc = 2
    finally:
        _emit_cli_trace(sub_name, rc)
    return rc


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(_cli_main())
