"""End-to-end pipeline orchestrator for L0 v5.

Spec: docs/l0_v5/MASTER_PLAN.md §11

Sequence:
  1. Stage 0/0b — bootstrap manifest from WeChat sqlite OR converted batches
  2. Convert legacy v3 prompt.json to v5 format (if --src-archive given)
  3. Pick N batches (smoke or full)
  4. Pass-1 dispatch to your-org + collect outputs (or local Mac LLM)
  5. Manifest merge (Stage 3+4+5)
  6. Pass-2 dispatch (with grown manifest as ground truth)
  7. POST cards to memory_full_v5
  8. Run verify_l0_v5_query.py

Usage:
  python run_e2e_pipeline.py --mode smoke --max-batches 100
  python run_e2e_pipeline.py --mode full

Phase modes:
  smoke (10-100 batches, single LLM Mac): test the pipeline end-to-end on small data
  full (all 1790+ batches, your-org dual-LLM): production run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Ensure memex root on sys.path for src.core imports
_MEMEX_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_MEMEX_ROOT) not in sys.path:
    sys.path.insert(0, str(_MEMEX_ROOT))
from typing import Any, Dict, List, Optional

logger = logging.getLogger("e2e")


def _step(n: int, name: str) -> None:
    logger.info(f"\n{'=' * 60}\n[STEP {n}] {name}\n{'=' * 60}")


def step1_bootstrap_manifest(args) -> Dict[str, Any]:
    _step(1, "Bootstrap Manifest (Stage 0b)")
    cmd = [
        sys.executable, "-m", "tools.manifest_bootstrap_s0b",
        "--src", str(args.input_batches),
        "--manifest-path", str(args.manifest_path),
        "--min-mentions", "2",
    ]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent.parent.parent)}
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        return {"step": 1, "ok": False, "error": r.stderr[-500:]}

    from src.core.identity_manifest import ManifestStore
    s = ManifestStore.load(str(args.manifest_path))
    stats = s.stats()
    return {"step": 1, "ok": True, "manifest_stats": stats}


def step2_convert_archive(args) -> Dict[str, Any]:
    _step(2, "Convert legacy archive to v5 input format")
    if args.input_batches.exists() and any(args.input_batches.iterdir()):
        n_existing = sum(1 for _ in args.input_batches.rglob("prompt.json"))
        if n_existing > 0:
            return {"step": 2, "ok": True, "skipped": True, "n_batches": n_existing}

    if not args.src_archive or not args.src_archive.exists():
        return {"step": 2, "ok": False, "error": "no src_archive"}

    cmd = [
        sys.executable, "data/l0_v5/code/convert_extract_archive_to_v5.py",
        "--src", str(args.src_archive),
        "--out", str(args.input_batches),
    ]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent.parent.parent)}
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        return {"step": 2, "ok": False, "error": r.stderr[-500:]}

    n = sum(1 for _ in args.input_batches.rglob("prompt.json"))
    return {"step": 2, "ok": True, "n_batches_converted": n}


def step3_select_batches(args) -> Dict[str, Any]:
    _step(3, f"Select batches (mode={args.mode})")
    all_batches = sorted(args.input_batches.rglob("prompt.json"))
    if args.mode == "smoke":
        selected = all_batches[: args.max_batches]
    else:
        selected = all_batches
    selected_dir = args.work_dir / "selected"
    selected_dir.mkdir(parents=True, exist_ok=True)
    # Copy as flat list under work_dir/selected/<batch_id>.json
    for sp in selected:
        bid = sp.parent.name
        target = selected_dir / f"{bid}.json"
        if target.exists():
            continue
        shutil.copy2(sp, target)
    return {"step": 3, "ok": True,
            "n_selected": len(selected),
            "selected_dir": str(selected_dir)}


def step4_pass1(args, selected_dir: Path) -> Dict[str, Any]:
    _step(4, "Pass-1 IdentityAssertion extraction")
    pass1_out = args.work_dir / "pass1_out"
    pass1_done = args.work_dir / "pass1_done"
    pass1_out.mkdir(parents=True, exist_ok=True)
    pass1_done.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "data/l0_v5/code/l0_worker_v2_ustc.py",
        "--pass", "1",
        "--batches-dir", str(selected_dir),
        "--done-dir", str(pass1_done),
        "--out-dir", str(pass1_out),
        "--extractor-url", args.extractor_url,
        "--extractor-model", args.extractor_model,
        "--concurrent", str(args.concurrent),
    ]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent.parent.parent)}
    r = subprocess.run(cmd, capture_output=False, timeout=3600, env=env)
    n_done = sum(1 for _ in pass1_done.glob("*.done"))
    return {"step": 4, "ok": r.returncode == 0,
            "exit_code": r.returncode,
            "n_done": n_done,
            "out_dir": str(pass1_out)}


def step5_merge_manifest(args, pass1_out: Path) -> Dict[str, Any]:
    _step(5, "Manifest Merge (Stage 3+4+5)")
    cmd = [
        sys.executable, "-m", "tools.manifest_merge",
        "--pass1-dir", str(pass1_out),
        "--manifest-path", str(args.manifest_path),
    ]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent.parent.parent)}
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        return {"step": 5, "ok": False, "error": r.stderr[-500:]}
    from src.core.identity_manifest import ManifestStore
    s = ManifestStore.load(str(args.manifest_path))
    return {"step": 5, "ok": True, "manifest_stats": s.stats()}


def step6_pass2(args, selected_dir: Path) -> Dict[str, Any]:
    _step(6, "Pass-2 main extraction (schema v2 cards)")
    cards_dir = args.work_dir / "cards_v2"
    pass2_done = args.work_dir / "pass2_done"
    cards_dir.mkdir(parents=True, exist_ok=True)
    pass2_done.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "data/l0_v5/code/l0_worker_v2_ustc.py",
        "--pass", "2",
        "--batches-dir", str(selected_dir),
        "--done-dir", str(pass2_done),
        "--out-dir", str(cards_dir),
        "--gatekeeper-url", args.gatekeeper_url,
        "--gatekeeper-model", args.gatekeeper_model,
        "--extractor-url", args.extractor_url,
        "--extractor-model", args.extractor_model,
        "--concurrent", str(args.concurrent),
    ]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent.parent.parent)}
    r = subprocess.run(cmd, capture_output=False, timeout=7200, env=env)
    n_done = sum(1 for _ in pass2_done.glob("*.done"))
    n_cards = 0
    for cf in cards_dir.glob("*.json"):
        try:
            d = json.loads(cf.read_text(encoding="utf-8"))
            n_cards += len(d.get("cards", []))
        except Exception:
            pass
    return {"step": 6, "ok": r.returncode == 0,
            "exit_code": r.returncode,
            "n_done": n_done,
            "n_cards": n_cards,
            "cards_dir": str(cards_dir)}


def _normalize_llm_card(card_dict: dict) -> dict:
    """Normalize LLM-emitted card dict to schema v2 required keys.

    LLMs often improvise field names. We tolerate common variants:
    - TimeResolution: original_text → surface_form; missing anchor_message_ts/resolution_method
    - Entity: missing surface_form (default to canonical_name)
    """
    # TimeResolution variants
    # 2026-05-10: also strip unknown keys (e.g. LLM-emitted "time_resolution",
    # "time_expression") before passing to TimeResolution(**t) which is strict.
    _TR_ALLOWED = {"surface_form", "resolved_start", "resolved_end",
                   "anchor_message_ts", "confidence", "resolution_method"}
    when_start_default = card_dict.get("when_start", "2026-01-01T00:00:00+08:00")

    import re as _re
    _DATE_ONLY = _re.compile(r"^\d{4}-\d{2}-\d{2}$")
    _TIME_ONLY = _re.compile(r"^\d{2}:\d{2}(:\d{2})?$")

    def _normalize_iso(v):
        """Coerce LLM date/time strings to full ISO 8601 with T+tz, or None.

        Common LLM emissions (2026-05-13 dead-letter sweep):
          'YYYY-MM-DD'      → 'YYYY-MM-DDT00:00:00+08:00'
          'HH:MM:SS'        → None (date-less time is meaningless without anchor)
          ''                → None
          None              → None (unchanged)
          'YYYY-MM-DDTHH:MM:SS' (no tz) → '...+08:00'
        """
        if v is None or v == "":
            return None
        if not isinstance(v, str):
            return None
        if _DATE_ONLY.match(v):
            return f"{v}T00:00:00+08:00"
        if _TIME_ONLY.match(v):
            return None
        # missing timezone but has T
        if "T" in v and "+" not in v and "Z" not in v and v[-6:].count(":") < 2:
            return f"{v}+08:00"
        return v

    fixed_trs = []
    for t in card_dict.get("time_resolutions", []):
        # 2026-05-13: LLM occasionally emits bare string instead of dict
        if isinstance(t, str):
            t = {"surface_form": t, "confidence": "unresolved"}
        if not isinstance(t, dict):
            continue
        if "surface_form" not in t and "original_text" in t:
            t["surface_form"] = t.pop("original_text")
        if "surface_form" not in t and "text" in t:
            t["surface_form"] = t.pop("text")
        if "surface_form" not in t and "time_expression" in t:
            t["surface_form"] = t.pop("time_expression")
        if "anchor_message_ts" not in t:
            t["anchor_message_ts"] = when_start_default
        if "resolution_method" not in t and "time_resolution" in t:
            t["resolution_method"] = t.pop("time_resolution")
        if "resolution_method" not in t:
            t["resolution_method"] = "llm_inferred"
        if "surface_form" not in t:
            t["surface_form"] = "?"  # fallback; required field
        if "confidence" not in t:
            t["confidence"] = "inferred"
        # 2026-05-13: normalize ISO format on resolved_start/end
        rs = _normalize_iso(t.get("resolved_start"))
        re_ = _normalize_iso(t.get("resolved_end"))
        # ensure resolved_start/end present
        if rs is None and re_ is None:
            # No usable date → must mark unresolved
            t["confidence"] = "unresolved"
            t["resolved_start"] = None
            t["resolved_end"] = None
        else:
            t["resolved_start"] = rs if rs is not None else re_
            t["resolved_end"] = re_ if re_ is not None else rs
            # bumped to inferred if was unresolved but we have a date
            if t.get("confidence") == "unresolved":
                t["confidence"] = "inferred"
        # Strip unknown keys to satisfy strict TimeResolution dataclass
        t_clean = {k: v for k, v in t.items() if k in _TR_ALLOWED}
        fixed_trs.append(t_clean)
    card_dict["time_resolutions"] = fixed_trs

    # 2026-05-13: top-level when_start/when_end ISO format normalize.
    # when_end is REQUIRED positional in MemoryCard.__init__ → must default to
    # a valid ISO string (use when_start as fallback) when None / missing.
    for wk in ("when_start", "when_end"):
        v = card_dict.get(wk)
        if isinstance(v, str):
            if _DATE_ONLY.match(v):
                card_dict[wk] = f"{v}T00:00:00+08:00"
            elif "T" in v and "+" not in v and "Z" not in v:
                card_dict[wk] = f"{v}+08:00"
        elif v is None or not isinstance(v, str):
            card_dict[wk] = when_start_default

    # 2026-05-13: defensive cleanup — list fields may contain bare strings
    # (LLM occasionally emits `["foo"]` instead of `[{"quote": "foo", ...}]`).
    # AttributeError trap: 'str' object has no attribute 'get'.
    for list_key in ("identity_assertions", "relation_assertions"):
        items = card_dict.get(list_key) or []
        card_dict[list_key] = [x for x in items if isinstance(x, dict)]

    # Entity normalization
    _ROLE_ALLOWED = {"subject", "object", "mentioned", "audience"}
    fixed_entities = []
    for e in card_dict.get("entities", []):
        if "surface_form" not in e:
            e["surface_form"] = e.get("canonical_name") or "?"
        if "role_in_card" not in e:
            e["role_in_card"] = "mentioned"
        # 2026-05-13: LLM emits 'relay'/'speaker'/'sender' etc — map to canonical
        role = (e.get("role_in_card") or "").lower()
        if role not in _ROLE_ALLOWED:
            _ROLE_MAP = {
                "relay": "mentioned", "speaker": "subject",
                "sender": "subject", "receiver": "object",
                "addressee": "audience", "recipient": "object",
                "other": "mentioned", "narrator": "mentioned",
            }
            e["role_in_card"] = _ROLE_MAP.get(role, "mentioned")
        if "resolution_confidence" not in e:
            e["resolution_confidence"] = "inferred"
        # accept None/empty canonical_name → fix
        if not e.get("canonical_name"):
            e["canonical_name"] = e.get("surface_form", "?unknown")
        # 2026-05-13: bank schema requires surface_form non-empty after strip.
        # LLM occasionally emits "" or whitespace. Repair: fall back to canonical_name,
        # finally "?unknown" — drop only if both are unusable.
        sf = (e.get("surface_form") or "").strip()
        if not sf:
            sf = (e.get("canonical_name") or "").strip() or "?unknown"
            e["surface_form"] = sf
        fixed_entities.append(e)
    card_dict["entities"] = fixed_entities

    # 2026-05-13 v3: types canonicalization (HANDOFF D.12 known issue).
    # LLM occasionally emits non-canonical types like 'request'/'inquiry'/
    # 'reminder'/'instruction'. MemoryCard validator rejects → 422 dead-letter.
    # Map to nearest canonical OR put in open_type_hint.
    _TYPE_CANONICAL = {"commitment", "announcement", "decision", "state",
                       "interaction", "report", "question", "media_share"}
    _TYPE_MAP = {
        "request": "question", "inquiry": "question", "ask": "question",
        "reminder": "announcement", "warning": "announcement",
        "instruction": "commitment", "task": "commitment", "todo": "commitment",
        "note": "report", "update": "report", "status_update": "state",
        "progress": "state", "summary": "report", "issue": "state",
    }
    raw_types = card_dict.get("types") or []
    if isinstance(raw_types, str):
        raw_types = [t.strip() for t in raw_types.split(",") if t.strip()]
    fixed_types = []
    open_hints = []
    for t in raw_types:
        tl = str(t).lower().strip()
        if tl in _TYPE_CANONICAL:
            fixed_types.append(tl)
        elif tl in _TYPE_MAP:
            fixed_types.append(_TYPE_MAP[tl])
        else:
            # Novel type → 'state' + record in open_type_hint
            fixed_types.append("state")
            open_hints.append(tl)
    # dedup preserving order
    seen_t = set()
    fixed_types = [t for t in fixed_types if not (t in seen_t or seen_t.add(t))]
    if not fixed_types:
        fixed_types = ["state"]
    card_dict["types"] = fixed_types
    # also update types_csv if present
    if "types_csv" in card_dict:
        card_dict["types_csv"] = ",".join(fixed_types)
    if open_hints and not card_dict.get("open_type_hint"):
        card_dict["open_type_hint"] = "|".join(open_hints)[:60]

    # 2026-05-13: hindsight bank schema repair — truncate/cap fields that
    # cron-observed dead-letter errors targeted (69/97 cc cards 2026-05-13 03:00):
    #   * evidence_quote too long (220 > 200)        → truncate 200
    #   * evidence_quotes max 5 items                → take first 5
    #   * salience_reason required and ≤60 chars     → fill+truncate
    #   * related_episode Input should be string     → list→take [0] or None
    eqs = card_dict.get("evidence_quotes") or []
    if isinstance(eqs, list):
        # Cap count
        eqs = eqs[:5]
        # Cap each quote length
        eqs = [(q[:200] if isinstance(q, str) else str(q)[:200]) for q in eqs]
        card_dict["evidence_quotes"] = eqs
    sr = card_dict.get("salience_reason")
    if not isinstance(sr, str) or not sr.strip():
        # required field → synthesize from when/score if missing
        sal = card_dict.get("salience")
        sr = f"salience={sal}" if sal is not None else "auto-filled"
    card_dict["salience_reason"] = sr[:60]
    re_field = card_dict.get("related_episode")
    if isinstance(re_field, list):
        # Bank expects a single string (or null) — pick first non-empty
        first = next((str(x) for x in re_field if x), None)
        card_dict["related_episode"] = first  # may be None
    elif isinstance(re_field, dict):
        # 2026-05-13 dead-letter root cause (14 qq + 2 cc .dead markers):
        # qwen3.6-reasoner emits `{"session_id": "<uuid>"}` instead of bare
        # string. Pydantic schema requires str|None → 422. Extract whichever
        # id-like key exists, else flatten to first value, else null.
        chosen = None
        for k in ("session_id", "episode_id", "id", "episode", "name"):
            v = re_field.get(k)
            if v:
                chosen = str(v)
                break
        if chosen is None:
            vals = [str(v) for v in re_field.values() if v]
            chosen = vals[0] if vals else None
        card_dict["related_episode"] = chosen
    elif re_field is not None and not isinstance(re_field, str):
        # Belt-and-suspenders: any other unexpected type → stringify or null
        card_dict["related_episode"] = str(re_field) if re_field else None

    # IdentityAssertion variants
    fixed_ias = []
    for a in card_dict.get("identity_assertions", []):
        # Drop empty / no-quote
        if not a.get("quote"):
            continue
        if "subject_wxid_hash" not in a:
            a["subject_wxid_hash"] = "?"
        if "asserted_relation" not in a:
            continue
        if "asserted_value" not in a:
            continue
        if "confidence" not in a:
            a["confidence"] = "inferred"
        fixed_ias.append(a)
    card_dict["identity_assertions"] = fixed_ias

    # RelationAssertion validation
    fixed_ras = []
    for r in card_dict.get("relation_assertions", []):
        if not r.get("quote") or not r.get("context"):
            continue
        if r.get("person_a") == r.get("person_b"):
            continue
        if "person_a" not in r or "person_b" not in r:
            continue
        if "confidence" not in r:
            r["confidence"] = "inferred"
        fixed_ras.append(r)
    card_dict["relation_assertions"] = fixed_ras

    # ensure schema_v=2
    card_dict["schema_v"] = 2
    return card_dict


def step7_post_to_hindsight(args, cards_dir: Path) -> Dict[str, Any]:
    _step(7, "POST cards to memory_full_v5")
    import httpx
    n_total = 0
    n_ok = 0
    n_fail = 0
    failures: List[Dict[str, Any]] = []

    from src.core.memory_card_v2 import MemoryCard

    for cf in sorted(cards_dir.glob("*.json")):
        try:
            d = json.loads(cf.read_text(encoding="utf-8"))
        except Exception:
            continue
        for card_dict in d.get("cards", []):
            try:
                # Normalize LLM output before construction
                card_dict = _normalize_llm_card(card_dict)
                # Reconstruct dataclasses
                from src.core.memory_card_v2 import (
                    Entity, IdentityAssertion, TimeResolution, RelationAssertion,
                )
                entities = [Entity(**e) for e in card_dict.get("entities", [])]
                ias = [IdentityAssertion(**a) for a in card_dict.get("identity_assertions", [])]
                trs = [TimeResolution(**t) for t in card_dict.get("time_resolutions", [])]
                ras = [RelationAssertion(**r) for r in card_dict.get("relation_assertions", [])]
                card_dict["entities"] = entities
                card_dict["identity_assertions"] = ias
                card_dict["time_resolutions"] = trs
                card_dict["relation_assertions"] = ras
                card = MemoryCard(**card_dict)
                payload = card.to_retain_payload()

                r = httpx.post(
                    f"{args.hindsight_url}/v1/default/banks/{args.bank_id}/memories",
                    json={"items": [payload], "async": False},
                    timeout=120.0,
                )
                if r.status_code in (200, 201, 202):
                    n_ok += 1
                else:
                    n_fail += 1
                    failures.append({
                        "card_id": card.card_id(),
                        "code": r.status_code,
                        "body": r.text[:200],
                    })
                n_total += 1
            except Exception as e:
                n_fail += 1
                failures.append({"err": str(e)[:200]})
    return {
        "step": 7, "ok": n_fail == 0,
        "n_total": n_total, "n_ok": n_ok, "n_fail": n_fail,
        "failures_sample": failures[:5],
    }


def step8_verify(args) -> Dict[str, Any]:
    _step(8, "Verify suite")
    cmd = [
        sys.executable, "data/l0_v5/code/verify_l0_v5_query.py",
        "--hindsight-url", args.hindsight_url,
        "--bank-id", args.bank_id,
        "--manifest-path", str(args.manifest_path),
        "--out-report", str(args.work_dir / "verify_report.json"),
    ]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent.parent.parent.parent)}
    r = subprocess.run(cmd, capture_output=False, timeout=600, env=env)
    return {"step": 8, "ok": r.returncode == 0, "exit_code": r.returncode}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["smoke", "full"], default="smoke")
    parser.add_argument("--max-batches", type=int, default=100,
                        help="smoke mode batch cap (default 100)")
    parser.add_argument("--src-archive", type=Path, default=Path("data/extract_archive"))
    parser.add_argument("--input-batches", type=Path, default=Path("data/l0_v5/input_batches"))
    parser.add_argument("--manifest-path", type=Path, default=Path("data/identity_manifest.yaml"))
    parser.add_argument("--work-dir", type=Path, default=Path("data/l0_v5/work"))
    parser.add_argument("--gatekeeper-url", type=str,
                        default="http://<remote-server-ip>:8001")
    parser.add_argument("--gatekeeper-model", type=str, default="memex-gatekeeper")
    parser.add_argument("--extractor-url", type=str,
                        default="http://<remote-server-ip>:8011")
    parser.add_argument("--extractor-model", type=str, default="memex-extractor")
    parser.add_argument("--hindsight-url", type=str,
                        default="http://127.0.0.1:8888")
    parser.add_argument("--bank-id", type=str, default="memory_full_v5")
    parser.add_argument("--concurrent", type=int, default=4)
    parser.add_argument("--skip-steps", type=str, default="",
                        help="Comma-separated step numbers to skip (e.g. '1,2')")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")

    skip_set = set(int(x) for x in args.skip_steps.split(",") if x.strip())
    args.work_dir.mkdir(parents=True, exist_ok=True)

    pipeline_log: List[Dict[str, Any]] = []
    t_start = time.time()

    # Step 2 first (convert), then 1 (manifest from converted)
    if 2 not in skip_set:
        result = step2_convert_archive(args)
        pipeline_log.append(result)
        if not result.get("ok"):
            logger.error(f"step 2 failed: {result}")
            return 1

    if 1 not in skip_set:
        result = step1_bootstrap_manifest(args)
        pipeline_log.append(result)
        if not result.get("ok"):
            logger.error(f"step 1 failed: {result}")
            return 1

    selected_dir = args.work_dir / "selected"
    if 3 not in skip_set:
        result = step3_select_batches(args)
        pipeline_log.append(result)
        if not result.get("ok"):
            return 1

    pass1_out = args.work_dir / "pass1_out"
    if 4 not in skip_set:
        result = step4_pass1(args, selected_dir)
        pipeline_log.append(result)
        if not result.get("ok"):
            logger.warning(f"step 4 issue: {result}")

    if 5 not in skip_set:
        result = step5_merge_manifest(args, pass1_out)
        pipeline_log.append(result)

    if 6 not in skip_set:
        result = step6_pass2(args, selected_dir)
        pipeline_log.append(result)

    cards_dir = args.work_dir / "cards_v2"
    if 7 not in skip_set:
        result = step7_post_to_hindsight(args, cards_dir)
        pipeline_log.append(result)

    if 8 not in skip_set:
        result = step8_verify(args)
        pipeline_log.append(result)

    # Final report
    total_s = time.time() - t_start
    summary = {
        "mode": args.mode,
        "duration_s": round(total_s, 1),
        "steps": pipeline_log,
        "all_ok": all(r.get("ok") for r in pipeline_log),
    }
    out = args.work_dir / "e2e_pipeline_report.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    logger.info(f"\n{'=' * 60}\nFINAL: all_ok={summary['all_ok']} duration={total_s:.0f}s\n"
                f"report: {out}\n{'=' * 60}")
    return 0 if summary["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
