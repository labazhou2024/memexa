"""
graph_memory.py -- Phase B: Neo4j-backed graph memory (2026-04-21)

Public API for writing/querying the knowledge graph. Sits on top of
`neo4j` Python driver directly (not via graphiti_client, which uses a
flat (:Fact) blob schema unsuitable for entity-centric queries).

Schema:
  (:Entity {canon, raw_forms, first_seen, last_seen, episode_count})
  (:Fact   {id, predicate, predicate_canon, source_episode_id,
            source_span, source_span_sha, confidence, tier,
            observed_at, valid_from, valid_to, invalidated,
            invalidate_reason})
  (:Episode {id, text, source, ts})  -- already exists from prior shadow seeding

  (:Entity)-[:SUBJECT_OF]->(:Fact)
  (:Fact)-[:HAS_OBJECT]->(:Entity)
  (:Fact)-[:FROM_EPISODE]->(:Episode)

Design principles:
- Fail-soft: never raise unless security boundary crossed. Return [] or
  None on operational errors, log once.
- Idempotent: re-ingesting the same memory file must produce the same
  graph state (uniqueness on (source_episode_id, source_span_sha)).
- No per-call subprocess: all writes batch within one session.
- Never mutate old (:Fact) blob nodes from graphiti_client — coexist.

CLI (verified alive in dev):
  python -m src.core.graph_memory ensure-schema
  python -m src.core.graph_memory stats
  python -m src.core.graph_memory ingest <memory-file>
  python -m src.core.graph_memory query <entity>
  python -m src.core.graph_memory path <A> <B>
  python -m src.core.graph_memory recent [--hours N]
  python -m src.core.graph_memory backfill [--limit N] [--dry-run]
  python -m src.core.graph_memory invalidate <fact_id> <reason>
"""
from __future__ import annotations


import argparse
import hashlib
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from src.core._path_resolver import memory_dir as _default_memory_dir

logger = logging.getLogger(__name__)


class ProductionGraphPollutionError(RuntimeError):
    """Raised by write_fact when tmp/test path targets production graph
    without opt-in. See H1 guard in write_fact docstring."""


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_driver = None
_driver_error: Optional[str] = None


def _get_driver():
    """Lazy singleton. Returns None on failure (fail-soft).

    TU-α2 (2026-04-21): honor MEMEXA_GRAPH_BACKEND env:
      - "blocked" (test tree default): raise RuntimeError immediately — any
        test path reaching production Neo4j is a bug (use an explicit
        opt-in fixture instead).
      - "none": return None without raising (legacy fail-soft).
      - unset or "neo4j": current behavior (connect to bolt:// URI).
    """
    global _driver, _driver_error
    backend = os.environ.get("MEMEXA_GRAPH_BACKEND", "neo4j").lower()
    if backend == "blocked":
        raise RuntimeError(
            "graph_memory._get_driver() called under MEMEXA_GRAPH_BACKEND=blocked. "
            "Tests must opt into an explicit backend fixture (e.g. mock_graph_driver) "
            "rather than hit production Neo4j. See tests/graph_closure/conftest.py."
        )
    if backend == "none":
        _driver_error = "MEMEXA_GRAPH_BACKEND=none"
        return None
    if _driver is not None:
        return _driver
    try:
        from neo4j import GraphDatabase  # lazy
        uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        user = os.environ.get("NEO4J_USER", "neo4j")
        pwd = os.environ.get("NEO4J_PASSWORD", "")
        if not pwd:
            _driver_error = "NEO4J_PASSWORD not set"
            return None
        d = GraphDatabase.driver(uri, auth=(user, pwd), connection_timeout=5.0)
        d.verify_connectivity()
        _driver = d
        return _driver
    except Exception as e:
        _driver_error = type(e).__name__ + ": " + str(e)[:200]
        logger.warning("graph_memory: connect failed: %s", _driver_error)
        return None


def close() -> None:
    global _driver
    if _driver is not None:
        try:
            _driver.close()
        except Exception:
            pass
        _driver = None


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class FactRow:
    """Flat read-view of a fact suitable for CLI printing."""
    fact_id: str
    subject_canon: str
    subject_raw: str
    predicate: str
    predicate_canon: str
    object_canon: str
    object_raw: str
    confidence: float
    tier: str
    source_episode_id: str
    source_span: str
    observed_at: str

    def fmt(self) -> str:
        src = Path(self.source_episode_id).name if self.source_episode_id else "?"
        return (
            f"[{self.confidence:.2f} {self.tier}] "
            f"{self.subject_raw or self.subject_canon} "
            f"--{self.predicate}--> "
            f"{self.object_raw or self.object_canon}  "
            f"(src: {src})"
        )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_STATEMENTS = [
    "CREATE INDEX entity_canon IF NOT EXISTS FOR (e:Entity) ON (e.canon)",
    # TU-P3 (2026-04-21): Entity.kind typing for filterable queries.
    "CREATE INDEX entity_kind IF NOT EXISTS FOR (e:Entity) ON (e.kind)",
    "CREATE INDEX fact_pred_canon IF NOT EXISTS FOR (f:Fact) ON (f.predicate_canon)",
    "CREATE INDEX fact_invalidated IF NOT EXISTS FOR (f:Fact) ON (f.invalidated)",
    "CREATE INDEX fact_source IF NOT EXISTS FOR (f:Fact) ON (f.source_episode_id)",
    "CREATE INDEX episode_source IF NOT EXISTS FOR (e:Episode) ON (e.source)",
    # Uniqueness constraint: prevents double-ingest of same (source_span_sha)
    # when re-running backfill. We use source_span_sha alone as the
    # uniqueness key — same span rewritten to a different episode is a
    # duplicate by definition.
    "CREATE CONSTRAINT fact_span_unique IF NOT EXISTS FOR (f:Fact) REQUIRE f.source_span_sha IS UNIQUE",
]


def ensure_schema() -> Dict[str, Any]:
    """Idempotent: creates indexes + constraint. Returns status dict."""
    d = _get_driver()
    if d is None:
        return {"ok": False, "error": _driver_error, "created": 0}
    created = 0
    errors: List[str] = []
    with d.session() as s:
        for stmt in _SCHEMA_STATEMENTS:
            try:
                s.run(stmt).consume()
                created += 1
            except Exception as e:
                errors.append(f"{stmt[:60]}: {type(e).__name__}")
    return {"ok": not errors, "created": created, "errors": errors}


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def _span_sha(source_episode_id: str, source_span: str) -> str:
    h = hashlib.sha256(
        (source_episode_id + "||" + source_span).encode("utf-8")
    ).hexdigest()
    return h[:16]


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def ensure_episode(source_episode_id: str,
                   text: Optional[str] = None) -> Optional[str]:
    """MERGE an Episode node identified by its source path. Returns the
    Episode's `id` property or None on failure.

    AC-G6 (2026-04-25): always sets `observed_at` (was previously None for
    many Episode nodes — direct Neo4j inspection 2026-04-25 showed 5/5
    most-recent Episodes had observed_at=None, breaking time-window queries).
    """
    d = _get_driver()
    if d is None:
        return None
    with d.session() as s:
        r = s.run(
            """
            MERGE (e:Episode {source: $source})
            ON CREATE SET e.id = $new_id,
                          e.ts = $now,
                          e.observed_at = $now,
                          e.text = $text
            ON MATCH  SET e.observed_at = coalesce(e.observed_at, $now)
            RETURN e.id AS id
            """,
            source=source_episode_id,
            new_id=f"ep_{uuid.uuid4().hex[:12]}",
            now=_now_iso(),
            text=(text or "")[:2000],
        ).single()
        return r["id"] if r else None


def _emit_write_fact_trace(event: str, payload: Dict[str, Any]) -> None:
    """AC-G2 (2026-04-25): emit write_fact lifecycle events.

    Fail-soft: never raises (matches the rest of write_fact's tolerance to
    trace_sink unavailability). 5 events total:
      write_fact_succeeded         — happy path, fact_id in payload
      write_fact_skipped_no_driver — Neo4j connection unavailable
      write_fact_skipped_empty_canon — subject_canon or object_canon empty
      write_fact_skipped_empty_span  — source_span empty
      write_fact_skipped_bad_input_type — input not Fact dataclass nor dict
      write_fact_skipped_merge_noop  — Cypher MERGE returned no row (rare)

    Diff (extract_done count) - (write_fact_succeeded count) = silent failure
    rate. This closes the writer-reader contract HARD RULE for the memory
    pipeline.
    """
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except (ImportError, OSError):
        pass  # fail-soft: trace failure must never break write_fact


def write_fact(
    fact: Any,
    source_episode_id: str,
    source_path: Optional[str] = None,
) -> Optional[str]:
    """Write a single Fact (from dual_llm_extractor) into the graph.

    Accepts either a `Fact` dataclass instance (with .subject, .predicate,
    .object_, .source_span, .confidence, .subject_canon, .predicate_canon,
    .object_canon) or a dict with equivalent keys.

    Idempotent via uniqueness on source_span_sha. Re-ingest → no duplicate.

    TU-α3 (2026-04-21) additions:
      - `source_path`: optional filesystem path of the .md file the fact came
        from. Persisted as `f.source_path` property for provenance-gated
        cleanup scripts (Phase β TU-β2 filter-delete).
      - predicate_canon is normalized via normalize_predicate_canon() before
        MERGE, so hyphen/space violations cannot reach the DB.

    Returns: fact_id (newly-created OR existing) or None on failure.

    2026-04-22 production pollution guard:
      Prevents tmp/test paths (source_episode_id starting with /tmp/,
      containing AppData\\Local\\Temp, /var/folders/, or pytest-of-) from
      being written into the production neo4j database (NEO4J_DATABASE
      unset or == 'neo4j').

      Escape hatch: set env MEMEXA_ALLOW_PRODUCTION_TMP=1 (legacy tests
      that cannot yet use isolated NEO4J_DATABASE). Escape logs a warning.
    """
    # Production-graph pollution guard (2026-04-22 H1 closure)
    _TMP_PATTERNS = ("/tmp/", "/var/folders/", "AppData\\Local\\Temp",
                     ":\\Temp\\", ":\\tmp\\", "pytest-of-")
    _prod_db = (os.environ.get("NEO4J_DATABASE", "neo4j") or "neo4j").lower() == "neo4j"
    _is_tmp = source_episode_id and any(p in source_episode_id for p in _TMP_PATTERNS)
    _allow = os.environ.get("MEMEXA_ALLOW_PRODUCTION_TMP", "0") == "1"
    if _prod_db and _is_tmp and not _allow:
        raise ProductionGraphPollutionError(
            f"Refusing write: tmp-path source_episode_id {source_episode_id!r} "
            f"into production graph (NEO4J_DATABASE={os.environ.get('NEO4J_DATABASE','neo4j')}). "
            f"Fix: set NEO4J_DATABASE=test_<scope> OR MEMEXA_ALLOW_PRODUCTION_TMP=1 (legacy opt-in)."
        )
    if _prod_db and _is_tmp and _allow:
        logger.warning(
            "write_fact: legacy opt-in (MEMEXA_ALLOW_PRODUCTION_TMP=1) allowed "
            "tmp-path into production graph: %s", source_episode_id[:120]
        )

    d = _get_driver()
    if d is None:
        _emit_write_fact_trace("write_fact_skipped_no_driver", {
            "source_episode_id": (source_episode_id or "")[:120],
        })
        return None

    # Normalize input to a dict
    if hasattr(fact, "subject_canon"):
        data = {
            "subject_raw": fact.subject,
            "subject_canon": fact.subject_canon,
            "predicate_raw": fact.predicate,
            "predicate_canon": fact.predicate_canon,
            "object_raw": fact.object_,
            "object_canon": fact.object_canon,
            "source_span": fact.source_span,
            "confidence": float(fact.confidence),
        }
    elif isinstance(fact, dict):
        data = {
            "subject_raw": fact.get("subject_raw") or fact.get("subject", ""),
            "subject_canon": fact.get("subject_canon") or fact.get("subject", ""),
            "predicate_raw": fact.get("predicate_raw") or fact.get("predicate", ""),
            "predicate_canon": fact.get("predicate_canon") or fact.get("predicate", ""),
            "object_raw": fact.get("object_raw") or fact.get("object", ""),
            "object_canon": fact.get("object_canon") or fact.get("object", ""),
            "source_span": fact.get("source_span", ""),
            "confidence": float(fact.get("confidence", 0.0)),
        }
    else:
        logger.warning("write_fact: unsupported input type %s", type(fact))
        _emit_write_fact_trace("write_fact_skipped_bad_input_type", {
            "input_type": type(fact).__name__,
        })
        return None

    if not data["subject_canon"] or not data["object_canon"]:
        _emit_write_fact_trace("write_fact_skipped_empty_canon", {
            "source_episode_id": (source_episode_id or "")[:120],
            "subject_empty": not data["subject_canon"],
            "object_empty": not data["object_canon"],
            "predicate": (data.get("predicate_raw") or "")[:60],
        })
        return None
    if not data["source_span"]:
        _emit_write_fact_trace("write_fact_skipped_empty_span", {
            "source_episode_id": (source_episode_id or "")[:120],
            "subject": (data.get("subject_raw") or "")[:60],
        })
        return None

    # TU-1 (2026-04-23): wire ENTITY canonicalizer at write-time.
    # Fixes alias-drift bug where query_entity("user_remote_user") returned 0 because
    # facts were MERGEd under raw-alias canon (e.g. "Alice") instead of canonical
    # (user_remote_user). We ONLY canonicalize subject + object (entities); predicate
    # canonicalization stays with normalize_predicate_canon below — changing
    # predicate alias mapping here breaks classify_predicate consumers + the
    # test_write_fact_normalizes_predicate_before_merge contract.
    try:
        from src.core.canonicalizer import canonicalize_entity as _canon_ent
        _s_c = _canon_ent(data["subject_canon"])
        _o_c = _canon_ent(data["object_canon"])
        if _s_c:
            data["subject_canon"] = _s_c
        if _o_c:
            data["object_canon"] = _o_c
    except Exception as _canon_err:
        logger.warning("canonicalize_entity failed (fail-soft, keeping raw canon): %s", _canon_err)

    # TU-α3 (2026-04-21): normalize predicate_canon to snake_case BEFORE any
    # downstream use. Fixes contamination from "sample pred" / "connected to".
    try:
        from src.core.predicate_semantics import normalize_predicate_canon
        _orig_canon = data.get("predicate_canon") or data.get("predicate_raw") or ""
        data["predicate_canon"] = normalize_predicate_canon(_orig_canon)
    except Exception as _e:
        logger.warning("predicate normalize failed: %s", _e)

    # TU-P4 (2026-04-21): reject mojibake residue at write time. U+FFFD is the
    # Unicode replacement character; its presence in any display field means
    # the source was decoded with the wrong codec upstream and the data is
    # corrupt. Do NOT persist; log once and drop.
    REPL = "\ufffd"
    for f_name in ("subject_raw", "object_raw", "predicate_raw", "source_span"):
        val = data.get(f_name) or ""
        if REPL in val:
            logger.warning(
                "write_fact: rejecting mojibake (%s contains U+FFFD): %r",
                f_name, (val or "")[:80],
            )
            return None

    # L-07 R1 fix (2026-04-21): hash must be computed over the same
    # string that gets stored (truncated to 400 chars). Previously the
    # hash used the full span while storage used the truncated form,
    # letting two different spans collide after truncation OR producing
    # different hashes for the same stored text depending on pre-trunc
    # length.
    data["source_span"] = data["source_span"][:400]
    span_sha = _span_sha(source_episode_id, data["source_span"])
    tier = fact.tier if hasattr(fact, "tier") else (
        fact.get("tier", "pending_review") if isinstance(fact, dict) else "pending_review"
    )

    # TU-2 (2026-04-23): repair_tier provenance. Extract the
    # _repair_tier marker that dual_llm_extractor._real_llm_call attaches
    # to each fact dict (1 = clean, 2 = json_repair salvage, 3 = substring +
    # repair). Tier-3 repairs are structurally riskiest; force pending_review
    # so they can never short-circuit into auto_write / ceo_approved.
    #
    # F2 F3 fix (2026-04-23 code-reviewer Stage 4):
    #   - F2: Also read _repair_tier when `fact` is a Fact dataclass instance
    #         (mainline extract_single_llm path goes dict→Fact→write_fact).
    #   - F3: Default to 1 (not None) so Cypher SET writes a property rather
    #         than removing it.
    repair_tier: int = 1
    if hasattr(fact, "_repair_tier"):
        rt_raw = getattr(fact, "_repair_tier", 1)
        if isinstance(rt_raw, int) and 1 <= rt_raw <= 3:
            repair_tier = rt_raw
    elif isinstance(fact, dict):
        rt_raw = fact.get("_repair_tier", 1)
        if isinstance(rt_raw, int) and 1 <= rt_raw <= 3:
            repair_tier = rt_raw
    if repair_tier == 3 and tier not in ("pending_review", "rejected"):
        logger.info(
            "write_fact: Tier-3 repair downgrading tier %s -> pending_review",
            tier,
        )
        tier = "pending_review"

    # Ensure Episode exists (atomically, under the same session)
    ep_id = ensure_episode(source_episode_id)
    if ep_id is None:
        logger.warning("write_fact: could not ensure episode for %s", source_episode_id)
        return None

    # TU-P3 (2026-04-21): classify entity kinds for first_seen tagging.
    try:
        from src.core.entity_kind import classify_entity as _ek
        subject_kind = _ek(data["subject_canon"], [data["subject_raw"]])
        object_kind = _ek(data["object_canon"], [data["object_raw"]])
    except Exception:
        subject_kind = "other"
        object_kind = "other"

    params = {
        "fact_id": f"f_{uuid.uuid4().hex[:12]}",
        "source_path": source_path or "",  # TU-α3 (2026-04-21)
        "subject_canon": data["subject_canon"],
        "subject_raw": data["subject_raw"],
        "subject_kind": subject_kind,
        "object_canon": data["object_canon"],
        "object_raw": data["object_raw"],
        "object_kind": object_kind,
        "predicate": data["predicate_raw"] or data["predicate_canon"],
        "predicate_canon": data["predicate_canon"],
        "source_episode_id": source_episode_id,
        "source_span": data["source_span"][:400],
        "source_span_sha": span_sha,
        "confidence": data["confidence"],
        "tier": tier,
        "repair_tier": repair_tier,  # TU-2 2026-04-23
        "now": _now_iso(),
    }

    cypher = """
    // MERGE subject + object entities by canonical form (entity resolution)
    MERGE (s:Entity {canon: $subject_canon})
      ON CREATE SET s.first_seen = $now,
                    s.raw_forms = [$subject_raw],
                    s.kind = $subject_kind
      ON MATCH  SET s.last_seen = $now,
                    s.raw_forms = CASE
                      WHEN $subject_raw IN coalesce(s.raw_forms, [])
                      THEN s.raw_forms
                      ELSE coalesce(s.raw_forms, []) + $subject_raw
                    END,
                    s.kind = coalesce(s.kind, $subject_kind)
    MERGE (o:Entity {canon: $object_canon})
      ON CREATE SET o.first_seen = $now,
                    o.raw_forms = [$object_raw],
                    o.kind = $object_kind
      ON MATCH  SET o.last_seen = $now,
                    o.raw_forms = CASE
                      WHEN $object_raw IN coalesce(o.raw_forms, [])
                      THEN o.raw_forms
                      ELSE coalesce(o.raw_forms, []) + $object_raw
                    END,
                    o.kind = coalesce(o.kind, $object_kind)

    // Dedup on source_span_sha. If a fact with that span exists, return it.
    MERGE (f:Fact {source_span_sha: $source_span_sha})
      ON CREATE SET f.id = $fact_id,
                    f.predicate = $predicate,
                    f.predicate_canon = $predicate_canon,
                    f.source_episode_id = $source_episode_id,
                    f.source_path = $source_path,
                    f.source_span = $source_span,
                    f.confidence = $confidence,
                    f.tier = $tier,
                    f.repair_tier = $repair_tier,
                    f.observed_at = $now,
                    f.valid_from = $now,
                    f.valid_to = null,
                    f.invalidated = false
      ON MATCH SET f.last_seen = $now,
                   // LOG-F1 R1 fix (2026-04-21): coalesce treats empty string as
                   // non-null, permanently blocking legitimate backfill. Use CASE
                   // to treat null OR "" as overridable.
                   f.source_path = CASE
                     WHEN f.source_path IS NULL OR f.source_path = ""
                     THEN $source_path
                     ELSE f.source_path
                   END

    // MERGE relationships only if they don't already exist
    MERGE (s)-[:SUBJECT_OF]->(f)
    MERGE (f)-[:HAS_OBJECT]->(o)
    WITH f
    MATCH (ep:Episode {source: $source_episode_id})
    MERGE (f)-[:FROM_EPISODE]->(ep)
    RETURN f.id AS id
    """

    try:
        with d.session() as s:
            r = s.run(cypher, **params).single()
            fact_id = r["id"] if r else None
            if fact_id is None:
                _emit_write_fact_trace("write_fact_skipped_merge_noop", {
                    "source_episode_id": (source_episode_id or "")[:120],
                    "predicate_canon": data.get("predicate_canon", "")[:60],
                })
                return None

            _maybe_supersede(
                session=s,
                data=data,
                tier=tier,
                new_fact_id=fact_id,
                now=params["now"],
            )

            _emit_write_fact_trace("write_fact_succeeded", {
                "fact_id": fact_id,
                "predicate_canon": data.get("predicate_canon", "")[:60],
                "source_episode_id": (source_episode_id or "")[:120],
                "source_path": (source_path or "")[:200],
            })
            return fact_id
    except Exception as e:
        # Uniqueness constraint violation is the expected idempotency
        # signal — convert to "already exists" fetch + supersession
        # pass (L-03 R1 fix: previously skipped supersession on this
        # path, leaving stale facts live when the new value differed).
        msg = str(e)
        if "already exists" in msg or "ConstraintValidation" in type(e).__name__:
            try:
                with d.session() as s:
                    r = s.run(
                        "MATCH (f:Fact {source_span_sha: $sha}) RETURN f.id AS id",
                        sha=span_sha,
                    ).single()
                    if r:
                        existing_id = r["id"]
                        _maybe_supersede(
                            session=s,
                            data=data,
                            tier=tier,
                            new_fact_id=existing_id,
                            now=params["now"],
                        )
                        return existing_id
            except Exception:
                pass
        logger.warning("write_fact failed: %s: %s", type(e).__name__, msg[:200])
        return None


def _maybe_supersede(
    session: Any,
    data: Dict[str, Any],
    tier: str,
    new_fact_id: str,
    now: str,
) -> None:
    """TU-P5 supersession helper. Predicate-aware, gated, fail-soft.

    Functional predicates (single-valued at a given time) invalidate
    older conflicting facts. Multi-valued and unknown predicates never
    trigger supersession (verifier blocker fix).

    L-03 R1 fix: extracted to a helper so BOTH the happy-path write and
    the span_sha collision idempotency path can invoke it. Previously
    the collision branch skipped supersession entirely.
    """
    try:
        from src.core.predicate_semantics import is_supersession_eligible
        if not (
            is_supersession_eligible(data["predicate_canon"])
            and tier == "auto_write"
            and data["confidence"] >= 0.7
        ):
            return
        superseded = session.run(
            """
            MATCH (subj:Entity {canon: $subject_canon})-[:SUBJECT_OF]->(old:Fact)
            WHERE old.predicate_canon = $predicate_canon
              AND old.id <> $new_fact_id
              AND (old.invalidated IS NULL OR old.invalidated = false)
              AND old.tier = 'auto_write'
              AND old.confidence >= 0.7
            MATCH (old)-[:HAS_OBJECT]->(old_o:Entity)
            WHERE old_o.canon <> $object_canon
            SET old.invalidated = true,
                old.invalidate_reason = 'superseded_by:' + $new_fact_id,
                old.valid_to = $now
            RETURN count(old) AS n
            """,
            subject_canon=data["subject_canon"],
            predicate_canon=data["predicate_canon"],
            object_canon=data["object_canon"],
            new_fact_id=new_fact_id,
            now=now,
        ).single()
        n = int(superseded["n"]) if superseded else 0
        if n > 0:
            logger.info(
                "write_fact: superseded %d prior fact(s) on (%s, %s)",
                n, data["subject_canon"], data["predicate_canon"],
            )
    except Exception as sup_e:
        # Supersession failure must NOT invalidate the parent write.
        logger.warning(
            "write_fact: supersession pass failed: %s", sup_e,
        )


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------

def _rec_to_factrow(rec: Dict[str, Any]) -> FactRow:
    f = rec.get("f") or {}
    s_canon = rec.get("s_canon") or ""
    s_raw = rec.get("s_raw") or ""
    o_canon = rec.get("o_canon") or ""
    o_raw = rec.get("o_raw") or ""
    return FactRow(
        fact_id=f.get("id", ""),
        subject_canon=s_canon,
        subject_raw=s_raw,
        predicate=f.get("predicate", ""),
        predicate_canon=f.get("predicate_canon", ""),
        object_canon=o_canon,
        object_raw=o_raw,
        confidence=float(f.get("confidence", 0.0)),
        tier=f.get("tier", ""),
        source_episode_id=f.get("source_episode_id", ""),
        source_span=f.get("source_span", ""),
        observed_at=f.get("observed_at", ""),
    )


def query_entity(query: str, limit: int = 20) -> List[FactRow]:
    """Return facts where query entity is subject OR object.

    Tries canonical form first (via canonicalizer.canonicalize_entity).
    Falls back to substring match on raw forms if canonical misses.
    """
    d = _get_driver()
    if d is None:
        return []
    try:
        from src.core.canonicalizer import canonicalize_entity, _normalize
        canon_form = canonicalize_entity(query)
    except Exception:
        canon_form = query.lower().strip()

    cypher = """
    MATCH (s:Entity)-[:SUBJECT_OF]->(f:Fact)-[:HAS_OBJECT]->(o:Entity)
    WHERE (f.invalidated IS NULL OR f.invalidated = false)
      AND (s.canon = $canon OR o.canon = $canon
           OR ANY(rf IN coalesce(s.raw_forms,[]) WHERE toLower(rf) CONTAINS toLower($q))
           OR ANY(rf IN coalesce(o.raw_forms,[]) WHERE toLower(rf) CONTAINS toLower($q)))
    RETURN f AS f,
           s.canon AS s_canon, head(s.raw_forms) AS s_raw,
           o.canon AS o_canon, head(o.raw_forms) AS o_raw
    ORDER BY f.confidence DESC, f.observed_at DESC
    LIMIT $limit
    """
    try:
        with d.session() as s:
            rows = [_rec_to_factrow(dict(r)) for r in
                    s.run(cypher, canon=canon_form, q=query, limit=int(limit))]
            return rows
    except Exception as e:
        logger.warning("query_entity failed: %s: %s", type(e).__name__, e)
        return []


def query_path(entity_a: str, entity_b: str, max_hops: int = 3) -> List[List[Dict[str, Any]]]:
    """Shortest path between two entities (via Fact relays)."""
    d = _get_driver()
    if d is None:
        return []
    try:
        from src.core.canonicalizer import canonicalize_entity
        ca = canonicalize_entity(entity_a)
        cb = canonicalize_entity(entity_b)
    except Exception:
        ca, cb = entity_a, entity_b

    # APOC-free shortest path via patterns
    cypher = """
    MATCH (a:Entity {canon: $ca}), (b:Entity {canon: $cb})
    MATCH p = shortestPath((a)-[:SUBJECT_OF|HAS_OBJECT*..$maxh]-(b))
    RETURN [n IN nodes(p) | CASE labels(n)[0]
              WHEN 'Entity' THEN {type:'Entity', canon: n.canon, raw: head(n.raw_forms)}
              WHEN 'Fact' THEN {type:'Fact', predicate: n.predicate, span: n.source_span}
            END] AS path
    """
    # Neo4j 5 requires literal bound on *..$n — replace literally
    cypher = cypher.replace("$maxh", str(int(max_hops) * 2))
    try:
        with d.session() as s:
            rows = s.run(cypher, ca=ca, cb=cb).data()
            return [r["path"] for r in rows]
    except Exception as e:
        logger.warning("query_path failed: %s", e)
        return []


def query_recent(hours: int = 24, limit: int = 50) -> List[FactRow]:
    d = _get_driver()
    if d is None:
        return []
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat() + "Z"
    cypher = """
    MATCH (s:Entity)-[:SUBJECT_OF]->(f:Fact)-[:HAS_OBJECT]->(o:Entity)
    WHERE f.observed_at > $since
      AND (f.invalidated IS NULL OR f.invalidated = false)
    RETURN f AS f,
           s.canon AS s_canon, head(s.raw_forms) AS s_raw,
           o.canon AS o_canon, head(o.raw_forms) AS o_raw
    ORDER BY f.observed_at DESC
    LIMIT $limit
    """
    try:
        with d.session() as s:
            return [_rec_to_factrow(dict(r)) for r in s.run(cypher, since=since, limit=int(limit))]
    except Exception as e:
        logger.warning("query_recent failed: %s", e)
        return []


def stats() -> Dict[str, Any]:
    d = _get_driver()
    if d is None:
        return {"ok": False, "error": _driver_error}
    out = {"ok": True}
    try:
        with d.session() as s:
            for label in ("Entity", "Fact", "Episode"):
                r = s.run(f"MATCH (n:{label}) RETURN count(n) AS c").single()
                out[label.lower() + "_count"] = int(r["c"])
            r = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()
            out["rel_count"] = int(r["c"])
            r = s.run(
                "MATCH (f:Fact) WHERE f.invalidated=true RETURN count(f) AS c"
            ).single()
            out["fact_invalidated"] = int(r["c"])
            # TU-P3 (2026-04-21): entity_kind breakdown
            try:
                kind_rows = s.run(
                    "MATCH (e:Entity) RETURN coalesce(e.kind,'unset') "
                    "AS k, count(e) AS n ORDER BY n DESC"
                ).data()
                out["entity_kind_breakdown"] = {
                    row["k"]: int(row["n"]) for row in kind_rows
                }
            except Exception:
                out["entity_kind_breakdown"] = {}
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def migrate_entity_kind(
    dry_run: bool = False,
    csv_path: Optional[str] = None,
    ambiguity_threshold: float = 0.5,
    strict: bool = True,
) -> Dict[str, Any]:
    """TU-P3 one-shot: fill Entity.kind for entities where it's null.

    Idempotent: re-runs skip already-labelled entities. Safe because
    classify_entity is deterministic and never errors (falls through to
    'other').

    TU-α4 (2026-04-21) additions:
      - `csv_path`: if provided, write a preview CSV with columns
        canon, raw_forms, proposed_kind. Enables CEO review before --apply.
      - `ambiguity_threshold`: if post-classification other/scanned exceeds
        this ratio AND strict=True, raise MigrationAmbiguousError instead
        of committing.
      - `strict`: default True; False bypasses the guard (escape hatch).
    """
    from src.core.entity_kind import classify_entity
    from src.core._migration_errors import MigrationAmbiguousError

    d = _get_driver()
    if d is None:
        return {"ok": False, "error": _driver_error}
    scanned = 0
    updated = 0
    breakdown: Dict[str, int] = {}
    # Track per-row proposals for CSV + ambiguity guard
    proposals: List[Tuple[str, list, str]] = []
    try:
        with d.session() as s:
            rows = s.run(
                "MATCH (e:Entity) WHERE e.kind IS NULL "
                "RETURN e.canon AS canon, coalesce(e.raw_forms,[]) AS raws"
            ).data()
            for row in rows:
                scanned += 1
                canon = row.get("canon") or ""
                raws = row.get("raws") or []
                kind = classify_entity(canon, raws)
                breakdown[kind] = breakdown.get(kind, 0) + 1
                proposals.append((canon, raws, kind))

        # TU-α4: write CSV preview (dry-run OR apply) for CEO audit trail
        if csv_path:
            try:
                import csv as _csv
                # SEC-R1-3 R1 fix (2026-04-21): path traversal guard. Resolve to
                # realpath and require containment under workspace root or a
                # plausible task dir. Refuse absolute paths outside that bound.
                csv_file = Path(csv_path).resolve()
                workspace_root = Path(__file__).resolve().parent.parent.parent.parent
                # Allow: under workspace, under user temp, or explicitly named
                # .claude/harness/tasks/* for multi-session artifacts.
                import tempfile as _tempfile
                tempdir = Path(_tempfile.gettempdir()).resolve()
                allowed_roots = [workspace_root, tempdir]
                ok_path = any(
                    str(csv_file).startswith(str(r)) for r in allowed_roots
                )
                if not ok_path:
                    raise ValueError(
                        f"SEC-R1-3: csv_path {csv_file} outside allowed "
                        f"roots {[str(r) for r in allowed_roots]}"
                    )
                csv_file.parent.mkdir(parents=True, exist_ok=True)
                with csv_file.open("w", encoding="utf-8", newline="") as cf:
                    w = _csv.writer(cf)
                    w.writerow(["canon", "raw_forms_head", "proposed_kind"])
                    for canon, raws, kind in proposals:
                        rf = "|".join(str(r) for r in (raws or [])[:3])
                        w.writerow([canon, rf, kind])
            except Exception as _e:
                logger.warning("migrate_entity_kind CSV write failed: %s", _e)

        # TU-α4: ambiguity guard
        other_count = breakdown.get("other", 0)
        ratio = (other_count / scanned) if scanned > 0 else 0.0
        if strict and ratio > ambiguity_threshold:
            top_unlabeled = [
                (c, r) for c, r, k in proposals if k == "other"
            ][:50]
            raise MigrationAmbiguousError(
                other_count=other_count,
                total_scanned=scanned,
                top_unlabeled=top_unlabeled,
                ratio_threshold=ambiguity_threshold,
            )

        # Apply (only if not dry-run)
        if not dry_run:
            with d.session() as s:
                for canon, _raws, kind in proposals:
                    s.run(
                        "MATCH (e:Entity {canon:$canon}) SET e.kind=$kind",
                        canon=canon, kind=kind,
                    )
                    updated += 1

    except MigrationAmbiguousError:
        raise  # propagate to caller
    except Exception as e:
        return {"ok": False, "error": str(e)[:200],
                "scanned": scanned, "updated": updated}
    return {
        "ok": True, "scanned": scanned, "updated": updated,
        "dry_run": dry_run, "breakdown": breakdown,
        "other_ratio": ratio,
        "csv_path": csv_path if csv_path else None,
    }


def audit_predicates() -> Dict[str, Any]:
    """TU-P5 support: enumerate live predicates with their classification.

    Surfaces predicates that are NOT in either registry so the CEO can
    decide whether each is functional or multi_valued. Until classified,
    unknowns default to multi_valued (safe, no supersession).
    """
    from src.core.predicate_semantics import classify_predicate
    d = _get_driver()
    if d is None:
        return {"ok": False, "error": _driver_error}
    try:
        with d.session() as s:
            rows = s.run(
                "MATCH (f:Fact) WHERE f.invalidated IS NULL OR f.invalidated=false "
                "RETURN coalesce(f.predicate_canon,'') AS p, count(f) AS n "
                "ORDER BY n DESC"
            ).data()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

    classified: Dict[str, List[Tuple[str, int]]] = {
        "functional": [], "multi_valued": [], "unknown": [],
    }
    total = 0
    for row in rows:
        p = (row.get("p") or "").strip()
        if not p:
            continue
        n = int(row.get("n") or 0)
        total += n
        cls = classify_predicate(p)
        classified[cls].append((p, n))
    summary = {
        "functional_count": sum(n for _, n in classified["functional"]),
        "multi_valued_count": sum(n for _, n in classified["multi_valued"]),
        "unknown_count": sum(n for _, n in classified["unknown"]),
        "total": total,
    }
    return {"ok": True, "summary": summary, "by_class": classified}


def invalidate_fact(fact_id: str, reason: str) -> bool:
    d = _get_driver()
    if d is None:
        return False
    try:
        with d.session() as s:
            r = s.run(
                """
                MATCH (f:Fact {id: $id})
                SET f.invalidated = true,
                    f.invalidate_reason = $r,
                    f.valid_to = $now
                RETURN f.id AS id
                """,
                id=fact_id, r=reason[:200], now=_now_iso(),
            ).single()
            return bool(r)
    except Exception as e:
        logger.warning("invalidate_fact failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Ingest pipeline (extract + write)
# ---------------------------------------------------------------------------

def ingest_file(path: Path, chunk_size: int = 800,
                model: str = "claude-sonnet-4-6") -> Dict[str, Any]:
    """Read a memory/*.md file, run extract_single_llm on its head chunk,
    write resulting facts to graph.

    v1: processes only the first chunk_size bytes of the file. Multi-chunk
    sliding window = Phase C.
    """
    from src.core.dual_llm_extractor import extract_single_llm
    if not path.exists() or not path.is_file():
        return {"ok": False, "reason": "file_missing", "path": str(path)}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "reason": f"read_error:{e}", "path": str(path)}
    chunk = text[:chunk_size]
    if not chunk.strip():
        return {"ok": False, "reason": "empty_file", "path": str(path)}

    # Ensure episode first so ingested facts can link
    ensure_episode(str(path.resolve()), text=chunk)

    result = extract_single_llm(chunk, source_episode_id=str(path.resolve()),
                                model=model)
    written = 0
    failed = 0
    # TU-α3 (2026-04-21): forward source_path so Fact nodes get provenance
    # for later filter-delete cleanup (Phase β TU-β2).
    _source_path = str(path.resolve())
    for f in result.facts:
        fid = write_fact(f, source_episode_id=_source_path,
                         source_path=_source_path)
        if fid:
            written += 1
        else:
            failed += 1

    return {
        "ok": True,
        "path": str(path.name),
        "status": result.status,
        "extracted": len(result.facts),
        "written": written,
        "failed": failed,
        "diag": result.diagnostics,
    }


def backfill(memory_dir: Optional[Path] = None,
             limit: Optional[int] = None,
             dry_run: bool = False,
             rate_limit_s: float = 1.0,
             skip_ingested: bool = True,
             since_iso: Optional[str] = None) -> Dict[str, Any]:
    """Loop all memory/*.md files, extract + write. Resumable via
    skip_ingested (checks if Episode with this source already has
    related facts).

    AC-G4 (2026-04-25): `since_iso` filters to files with mtime >= cutoff,
    targeting the 2026-04-24T15:44Z+ blackout window. Format: ISO-8601.
    Invalid format → ValueError raised (caller can fall back to full).
    """
    if memory_dir is None:
        memory_dir = _default_memory_dir()
    memory_dir = Path(memory_dir)
    if not memory_dir.exists():
        return {"ok": False, "reason": "memory_dir_missing", "dir": str(memory_dir)}

    # AC-G4: parse since_iso into epoch seconds for mtime comparison
    since_epoch: Optional[float] = None
    if since_iso:
        try:
            s = since_iso.replace("Z", "")
            if "+" in s[10:]:
                s = s.split("+", 1)[0]
            since_dt = datetime.fromisoformat(s)
            if since_dt.tzinfo is None:
                from datetime import timezone as _tz
                since_dt = since_dt.replace(tzinfo=_tz.utc)
            since_epoch = since_dt.timestamp()
        except (ValueError, AttributeError) as e:
            raise ValueError(f"invalid --since ISO-8601: {since_iso!r} ({e})") from e

    # Scan for already-ingested sources (have ≥1 linked Fact)
    ingested_sources: set = set()
    if skip_ingested and not dry_run:
        d = _get_driver()
        if d is not None:
            try:
                with d.session() as s:
                    for r in s.run(
                        """
                        MATCH (ep:Episode)<-[:FROM_EPISODE]-(f:Fact)
                        RETURN DISTINCT ep.source AS src
                        """
                    ):
                        ingested_sources.add(r["src"])
            except Exception as e:
                logger.warning("backfill: ingested scan failed: %s", e)

    files = sorted(memory_dir.glob("*.md"))
    # MEMORY.md is the index, not a memory file
    files = [f for f in files if f.name != "MEMORY.md"]
    # AC-G4: optional since_iso mtime filter
    if since_epoch is not None:
        filtered: List[Path] = []
        for f in files:
            try:
                if f.stat().st_mtime >= since_epoch:
                    filtered.append(f)
            except OSError:
                continue
        files = filtered
    if limit:
        files = files[:int(limit)]

    total_written = 0
    per_file: List[Dict[str, Any]] = []
    for i, f in enumerate(files, 1):
        src_abs = str(f.resolve())
        if src_abs in ingested_sources:
            per_file.append({"path": f.name, "skipped": "already_ingested"})
            continue
        if dry_run:
            per_file.append({"path": f.name, "dry_run": True})
            continue
        try:
            r = ingest_file(f)
            per_file.append(r)
            total_written += r.get("written", 0)
        except Exception as e:
            per_file.append({"path": f.name, "error": str(e)[:200]})
        if rate_limit_s > 0 and i < len(files):
            time.sleep(rate_limit_s)

    return {
        "ok": True,
        "dir": str(memory_dir),
        "total_files": len(files),
        "total_written": total_written,
        "skipped": sum(1 for r in per_file if r.get("skipped")),
        "errors": sum(1 for r in per_file if "error" in r or not r.get("ok", True)),
        "per_file": per_file,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli(argv: List[str]) -> int:
    # TU-A-revised (2026-04-24 plan_v2 AC-1): ensure CJK characters print
    # correctly on Windows terminals. Default Python stdout on Windows
    # uses cp936 which mangles any Unicode above U+4E00. Reconfigure
    # once at CLI entry to UTF-8 so query/recent/stats output displays
    # Chinese entity canons + predicate text faithfully.
    import sys as _sys
    if _sys.platform == "win32":
        try:
            _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # older Python / redirected streams — best effort

    parser = argparse.ArgumentParser(prog="graph_memory")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ensure-schema", help="create indexes + constraint (idempotent)")
    sub.add_parser("stats", help="node + rel counts")

    p_q = sub.add_parser("query", help="find facts about an entity")
    p_q.add_argument("entity")
    p_q.add_argument("--limit", type=int, default=20)

    p_p = sub.add_parser("path", help="shortest path between two entities")
    p_p.add_argument("a")
    p_p.add_argument("b")
    p_p.add_argument("--max-hops", type=int, default=3)

    p_r = sub.add_parser("recent", help="facts learned recently")
    p_r.add_argument("--hours", type=int, default=24)
    p_r.add_argument("--limit", type=int, default=50)

    p_i = sub.add_parser("ingest", help="extract + write facts from one memory file")
    p_i.add_argument("file")
    p_i.add_argument("--chunk", type=int, default=800)

    p_b = sub.add_parser("backfill", help="ingest all memory/*.md")
    p_b.add_argument("--limit", type=int, default=None)
    p_b.add_argument("--dry-run", action="store_true")
    p_b.add_argument("--rate", type=float, default=1.0)
    p_b.add_argument("--since", default=None,
                     help="ISO-8601 cutoff; only ingest files with mtime>=since")

    p_inv = sub.add_parser("invalidate", help="mark a fact invalid")
    p_inv.add_argument("fact_id")
    p_inv.add_argument("reason")

    # TU-P3 + TU-α4 (2026-04-21)
    p_mk = sub.add_parser("migrate-entity-kind",
                          help="backfill Entity.kind for unset entities")
    p_mk.add_argument("--dry-run", action="store_true")
    p_mk.add_argument("--csv", dest="csv_path", default=None,
                      help="TU-α4: write {canon,raw_forms,proposed_kind} CSV preview")
    p_mk.add_argument("--ambiguity-threshold", type=float, default=0.5,
                      help="TU-α4: other/scanned ratio above which to raise")
    p_mk.add_argument("--force", action="store_true",
                      help="TU-α4: bypass ambiguity guard (strict=False)")

    # TU-P5
    sub.add_parser("audit-predicates",
                   help="list live predicates with arity classification")

    args = parser.parse_args(argv[1:])

    if args.cmd == "ensure-schema":
        r = ensure_schema()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["ok"] else 1
    if args.cmd == "stats":
        r = stats()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    if args.cmd == "query":
        rows = query_entity(args.entity, limit=args.limit)
        if not rows:
            print(f"(no facts for {args.entity!r})")
            return 0
        for row in rows:
            print(row.fmt())
        print(f"\n-- {len(rows)} fact(s) --")
        return 0
    if args.cmd == "path":
        paths = query_path(args.a, args.b, max_hops=args.max_hops)
        if not paths:
            print(f"(no path from {args.a!r} to {args.b!r} within {args.max_hops} hops)")
            return 0
        for p in paths:
            print(" -> ".join(
                (f"[Entity {n.get('canon')}]" if n and n.get('type') == 'Entity'
                 else f"[Fact {n.get('predicate')}]" if n else "[?]")
                for n in p
            ))
        return 0
    if args.cmd == "recent":
        rows = query_recent(hours=args.hours, limit=args.limit)
        for row in rows:
            print(row.fmt())
        print(f"\n-- {len(rows)} fact(s) since {args.hours}h ago --")
        return 0
    if args.cmd == "ingest":
        r = ingest_file(Path(args.file), chunk_size=args.chunk)
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return 0 if r.get("ok") else 1
    if args.cmd == "backfill":
        r = backfill(limit=args.limit, dry_run=args.dry_run, rate_limit_s=args.rate,
                     since_iso=args.since)
        # Compact summary
        summary = {k: v for k, v in r.items() if k != "per_file"}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        # Per-file only if error
        errors = [f for f in r.get("per_file", []) if "error" in f or not f.get("ok", True)]
        if errors:
            print("\nErrors:")
            for e in errors[:10]:
                print(f"  {e}")
        return 0 if r.get("ok") else 1
    if args.cmd == "invalidate":
        ok = invalidate_fact(args.fact_id, args.reason)
        print(json.dumps({"invalidated": ok, "fact_id": args.fact_id}, ensure_ascii=False))
        return 0 if ok else 1
    if args.cmd == "migrate-entity-kind":
        from src.core._migration_errors import MigrationAmbiguousError
        try:
            r = migrate_entity_kind(
                dry_run=args.dry_run,
                csv_path=getattr(args, "csv_path", None),
                ambiguity_threshold=getattr(args, "ambiguity_threshold", 0.5),
                strict=not getattr(args, "force", False),
            )
        except MigrationAmbiguousError as e:
            err = {
                "ok": False,
                "error": "MigrationAmbiguousError",
                "other_count": e.other_count,
                "total_scanned": e.total_scanned,
                "ratio": round(e.ratio, 4),
                "threshold": e.ratio_threshold,
                "top_unlabeled_sample": [
                    {"canon": c, "raw_forms": r} for c, r in e.top_unlabeled[:10]
                ],
                "recovery": "Hand-label top_unlabeled then re-run with --force, "
                            "or expand entity_kind token banks.",
            }
            print(json.dumps(err, ensure_ascii=False, indent=2))
            return 2
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    if args.cmd == "audit-predicates":
        r = audit_predicates()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r.get("ok") else 1
    return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
