"""Convert v1 SPO facts (pair.jsonl) from extract_archive_email_browser/
to V2 envelope cards and POST to memory_full_v5.

No LLM needed — purely structural conversion. Idempotent via .posted markers.

Source compat: bypasses MemoryCard validation to allow source=browser_session/
browser_search (not in VALID_SOURCES). Server-side hindsight accepts arbitrary
metadata['source'] strings — they're just JSONB values, not enum-validated.
"""
from __future__ import annotations

import argparse
import base64 as _b64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HEADER_BEGIN = "【MEMORYCARD_V2_HEADER_BEGIN】"
HEADER_END = "【MEMORYCARD_V2_HEADER_END】"

DEFAULT_URL = os.environ.get("MEMEX_HINDSIGHT_URL", "http://127.0.0.1:8888")
DEFAULT_BANK = os.environ.get("MEMEX_HINDSIGHT_BANK", "memory_full_v5")

# v1 type → V2 CANONICAL_TYPES + open_type_hint
TYPE_MAP = {
    "attachment_share": ("share", None),
    "announcement": ("announcement", None),
    "request": ("question", None),
    "deadline": ("commitment", None),
    "meeting_invite": ("announcement", "meeting_invite"),
    "document_revision": ("share", "document_revision"),
    "system_notification": ("announcement", "system_notification"),
    "development": ("state", "development"),
    "research": ("state", "research"),
    "communication": ("interaction", None),
    "admin": ("state", "admin"),
    "navigation": ("state", "navigation"),
    "study": ("state", "study"),
    "entertainment": ("state", "entertainment"),
    "shopping": ("state", "shopping"),
    "social_media": ("state", "social_media"),
    "news": ("report", None),
    "security": ("report", "security"),
}

# v1 attestation tier → V2
ATTEST_MAP = {
    "paired_v1": "paired_v2",
    "single_qwen_v1": "single_qwen3_v2",
    "single_27b_v1": "single_gemma4_31b_v2",
    "single_gemma4_31b_v1": "single_gemma4_31b_v2",
    "ds_fallback_v1": "ds_fallback_v2",
    "rag_assisted_v1": "rag_assisted_v2",
}


def _hash16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _b64s(s: str) -> str:
    return _b64.b64encode(s.encode("utf-8")).decode("ascii")


def build_payload(
    fact: Dict[str, Any],
    batch_id: str,
    source_kind: str,
    extraction_prompt_sha: str,
) -> Optional[Dict[str, Any]]:
    """Build hindsight retain payload from a v1 SPO fact dict."""
    s_text = (fact.get("s") or fact.get("subject") or "").strip()
    p_text = (fact.get("p") or fact.get("predicate") or "").strip()
    o_text = (fact.get("o") or fact.get("object") or "").strip()
    if not (s_text and p_text and o_text):
        return None

    evidence = (fact.get("evidence_quote") or "").strip()
    if not evidence:
        evidence = f"{s_text} {p_text} {o_text}"
    evidence = evidence[:200]

    when_start = fact.get("when_start") or "2026-01-01T00:00:00+08:00"
    if "T" not in when_start:
        when_start = when_start + "T00:00:00+08:00"
    when_end = fact.get("when_end") or when_start

    v1_type = fact.get("type") or "state"
    type_v2, open_hint = TYPE_MAP.get(v1_type, ("state", v1_type))
    types = [type_v2]

    salience = float(fact.get("salience") or 0.5)
    if salience < 0.0 or salience > 1.0:
        salience = max(0.0, min(1.0, salience))
    sal_reason = (v1_type + "/" + str(fact.get("batch_type") or ""))[:60]

    v1_attest = fact.get("paired_eval_attested") or "paired_v1"
    attest = ATTEST_MAP.get(v1_attest, "paired_v2")

    # source
    if source_kind == "email":
        source = "email"
        speaker = "third_party"
        room_name = "email_thread"
    elif source_kind == "browser_session":
        source = "browser_session"
        speaker = "self"
        room_name = "browser_session"
    elif source_kind == "browser_search":
        source = "browser_search"
        speaker = "self"
        room_name = "browser_search"
    else:
        source = "doc"
        speaker = "document"
        room_name = source_kind or "unknown"

    room_hash = _hash16(f"{source}_{batch_id[:8]}")
    room_tier = 1

    # Build narrative (must be 30-1200 chars)
    narrative = (
        f"{s_text} {p_text} {o_text}。"
        f"时间: {when_start[:19]}。"
        f"来源: {source}。"
        f"证据: {evidence[:300]}"
    )
    if len(narrative) < 30:
        narrative = narrative + " (extracted from " + source + ")"
    narrative = narrative[:1200]

    # entities
    entities_full = [
        {
            "canonical_name": s_text[:200],
            "canonical_id": None,
            "role_in_card": "subject",
            "surface_form": s_text[:200],
            "resolution_confidence": "ambiguous",
            "sender_wxid_hash": None,
        },
        {
            "canonical_name": o_text[:200],
            "canonical_id": None,
            "role_in_card": "object",
            "surface_form": o_text[:200],
            "resolution_confidence": "ambiguous",
            "sender_wxid_hash": None,
        },
    ]
    entities_full_b64 = _b64s(json.dumps(entities_full, ensure_ascii=False, separators=(",", ":")))
    entities_hash_csv = ",".join(_hash16(e["canonical_name"]) for e in entities_full)

    # Inline V2 envelope content
    full_envelope = {
        "narrative": narrative,
        "evidence_quotes": [evidence],
        "when_start": when_start,
        "when_end": when_end,
        "where_chat_room": room_name,
        "where_chat_room_hash": room_hash,
        "room_tier": room_tier,
        "entities": entities_full,
        "speaker_role": speaker,
        "types": types,
        "salience": salience,
        "salience_reason": sal_reason,
        "attestation_tier": attest,
        "batch_id": batch_id,
        "extraction_prompt_sha": extraction_prompt_sha,
        "source": source,
        "schema_v": 2,
        "open_type_hint": open_hint,
        "supersedes": [],
        "answers": None,
        "related_episode": None,
        "identity_assertions": [],
        "time_resolutions": [],
        "relation_assertions": [],
        "unresolved_references": [],
    }
    content = (
        HEADER_BEGIN + "\n"
        + json.dumps(full_envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n" + HEADER_END + "\n\n"
        + narrative
    )

    # ASCII tags
    tags = [
        "kind:event",
        f"source:{source}",
        f"tier:{room_tier}",
        f"room:{room_hash[:16]}",
        f"speaker:{speaker}",
        f"attest:{attest}",
        "schema:v2",
    ] + [f"type:{t}" for t in types]
    if open_hint:
        # ASCII slug
        slug = "".join(c if (c.isascii() and c.isalnum()) else "_" for c in open_hint)[:40]
        if slug:
            tags.append(f"opentype:{slug}")
    # entity tags
    for e in entities_full:
        h = _hash16(e["canonical_name"])
        tags.append(f"entity:{h}")

    metadata = {
        "schema_v": "2",
        "salience": f"{salience:.3f}",
        "salience_reason_b64": _b64s(sal_reason),
        "speaker_role": speaker,
        "where_chat_room_hash": room_hash,
        "room_tier": str(room_tier),
        "when_start": when_start,
        "when_end": when_end,
        "types_csv": ",".join(types),
        "attestation_tier": attest,
        "batch_id": batch_id,
        "extraction_prompt_sha": extraction_prompt_sha,
        "source": source,
        "evidence_quotes_count": "1",
        "n_entities": str(len(entities_full)),
        "entities_hash_csv": entities_hash_csv,
        "entities_canonical_ids_csv": "",
        "entities_full_b64": entities_full_b64,
        "n_identity_assertions": "0",
        "n_time_resolutions": "0",
        "n_relation_assertions": "0",
        "n_unresolved": "0",
    }

    # ASCII verify
    for k, v in metadata.items():
        if any(ord(c) > 127 for c in str(k)) or any(ord(c) > 127 for c in str(v)):
            return None

    return {"content": content, "tags": tags, "metadata": metadata}


_HTTP_CLIENT = None


def _get_client(timeout: int = 60):
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        import httpx
        _HTTP_CLIENT = httpx.Client(timeout=timeout)
    return _HTTP_CLIENT


def post_one(payload: Dict[str, Any], url: str, bank: str, timeout: int = 60) -> Tuple[bool, str]:
    """POST via httpx to match streaming_post_v5 (async=False, json= serializes
    UTF-8 cleanly; matches the working wechat redo path)."""
    client = _get_client(timeout)
    try:
        r = client.post(
            f"{url}/v1/default/banks/{bank}/memories",
            json={"items": [payload], "async": False},
        )
        if r.status_code in (200, 201, 202):
            return (True, str(r.status_code))
        return (False, f"{r.status_code}: {r.text[:200]}")
    except Exception as e:
        return (False, f"{type(e).__name__}: {str(e)[:200]}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--src", default="data/extract_archive_email_browser",
                   help="dir with <date>/<batch>/pair.jsonl + prompt.json")
    p.add_argument("--marker-dir", default="data/l0_v5/work/posted_v1_email_browser",
                   help="batch-level idempotency markers")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--bank", default=DEFAULT_BANK)
    p.add_argument("--max-batches", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    src = Path(args.src)
    marker_dir = Path(args.marker_dir)
    marker_dir.mkdir(parents=True, exist_ok=True)

    # collect (batch_id, prompt_path, pair_path)
    batches: List[Tuple[str, Path, Path]] = []
    for date_dir in sorted(src.iterdir()):
        if not date_dir.is_dir():
            continue
        for bd in date_dir.iterdir():
            if not bd.is_dir():
                continue
            pp = bd / "pair.jsonl"
            pr = bd / "prompt.json"
            if pp.exists() and pr.exists():
                batches.append((bd.name, pr, pp))

    print(f"Found {len(batches)} batches in {src}")
    print(f"Bank: {args.bank}  URL: {args.url}  dry-run: {args.dry_run}")

    n_posted = 0
    n_failed = 0
    n_skipped_done = 0
    n_skipped_invalid = 0

    t0 = time.time()
    for i, (batch_id, prompt_path, pair_path) in enumerate(batches):
        if args.max_batches and i >= args.max_batches:
            break
        marker = marker_dir / f"{batch_id}.posted"
        if marker.exists():
            n_skipped_done += 1
            continue

        try:
            prompt_d = json.loads(prompt_path.read_text(encoding="utf-8"))
        except Exception:
            n_skipped_invalid += 1
            continue
        source_kind = prompt_d.get("source_kind", "unknown")
        extraction_prompt_sha = prompt_d.get("prompt_id", batch_id) or "v1_email_browser"

        # parse pair.jsonl
        facts = []
        for line in pair_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                facts.append(json.loads(line))
            except Exception:
                continue

        if not facts:
            marker.write_text("0", encoding="utf-8")
            continue

        ok_in_batch = 0
        fail_in_batch = 0
        for fact in facts:
            payload = build_payload(fact, batch_id, source_kind, extraction_prompt_sha)
            if payload is None:
                n_skipped_invalid += 1
                continue
            if args.dry_run:
                ok_in_batch += 1
                continue
            ok, msg = post_one(payload, args.url, args.bank)
            if ok:
                ok_in_batch += 1
                n_posted += 1
            else:
                fail_in_batch += 1
                n_failed += 1
                if n_failed <= 5:
                    print(f"  fail [{batch_id}]: {msg}")

        marker.write_text(f"{ok_in_batch}/{len(facts)}", encoding="utf-8")

        if (i + 1) % 100 == 0:
            elapsed = time.time() - t0
            rate = n_posted / max(elapsed, 1)
            print(f"  [{i+1}/{len(batches)}] posted={n_posted} failed={n_failed} skip_done={n_skipped_done} rate={rate:.1f}/s")

    elapsed = time.time() - t0
    print(f"\n=== DONE in {elapsed:.0f}s ===")
    print(f"  batches: {len(batches)}")
    print(f"  posted: {n_posted}")
    print(f"  failed: {n_failed}")
    print(f"  skip_done: {n_skipped_done}")
    print(f"  skip_invalid: {n_skipped_invalid}")
    return 0 if n_posted > 0 or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
