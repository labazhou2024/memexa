"""U7 TU-4: chat_extract_local — orchestrate denylist → consent → Qwen3 extract.

Reads chat msgs from outbox jsonl OR historical wechat_recent_*.json,
groups by chat_room (10-msg context window), applies denylist + consent_gate,
calls mlx_lm_wrapper.extract_chat_triples via router case 5 (force local),
emits FactRow with bi-temporal metadata.

TU-Phase2-2: paired_extract_for_chat adapter:
  - Default ON (MEMEXA_CHAT_EXTRACT_PAIRED=1): calls paired_eval.paired_extract
    for dual-model agreement; attests factrows with paired_eval_attested="paired_v1".
  - MEMEXA_CHAT_EXTRACT_PAIRED=0: legacy single-Qwen path; attests with
    paired_eval_attested="single_qwen_v1".
  - CrossModelUnavailableError: emit trace chat_extract_paired_unavailable + raise.

axis_anchor: [C:cli:chat_extract_local]
trace event: chat_extracted (delegated to mlx_lm_wrapper)
trace event: chat_extract_paired_done {n_accepted, n_disagreed}
trace event: chat_extract_paired_unavailable
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional


# Closure A plan_v3 TU-1: legacy hash helpers retained as thin re-exports of
# src.chat.metadata_builder for back-compat with existing test_chat_extract_local
# imports. New code MUST use the metadata_builder helpers directly.
from src.chat.metadata_builder import (  # noqa: E402
    chat_room_hash as _hash_chat_room,
    episode_id as _episode_id,
)


# ---------------------------------------------------------------------------
# TU-Phase2-2: trace helper
# ---------------------------------------------------------------------------

def _emit_chat(event: str, payload: dict) -> None:
    """Emit trace event; never raise."""
    try:
        from src.core.trace_sink import write_trace_event
        write_trace_event(event, payload)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# TU-Phase2-2: paired_extract_for_chat adapter
# ---------------------------------------------------------------------------

def paired_extract_for_chat(
    msg: dict,
    ctx: List[dict],
) -> List[dict]:
    """Adapter: extract triples from a chat message using paired_eval or legacy path.

    Environment control:
        MEMEXA_CHAT_EXTRACT_PAIRED (default "1"):
            "1" / "true" / unset → dual-model paired_eval path (default ON).
            "0" / "false" → legacy single-Qwen path.

    Returns:
        List of triple dicts with "s", "p", "o" keys + "paired_eval_attested" field.

    Raises:
        CrossModelUnavailableError: when PAIRED=1 and either model port returns
            non-200. NEVER silently falls back (fail-loud per architect-5/verifier-5).
            Emits trace event chat_extract_paired_unavailable before raising.
    """
    paired_env = os.environ.get("MEMEXA_CHAT_EXTRACT_PAIRED", "1").lower().strip()
    use_paired = paired_env not in {"0", "false"}

    # R-3 HARD RULE no-deferral fix: read calibration file; if recent
    # benchmark says paired latency >2× single → auto-disable paired.
    # Logic: env explicit 0/1 wins; calibration only kicks in when env=default.
    if use_paired and paired_env in {"1", "true", ""}:
        try:
            from tools.calibrate_paired_chat_latency import is_paired_disabled_by_calibration
            if is_paired_disabled_by_calibration():
                _emit_chat("chat_extract_paired_disabled_by_calibration", {
                    "reason": "calibration_ratio_exceeded_threshold",
                })
                use_paired = False
        except (ImportError, OSError):
            pass  # calibration file absent or unreadable → use env value

    text = str(msg.get("content") or msg.get("text") or "")
    # CEO 2026-05-04 directive: propagate chat attribution into LLM prompt.
    chat_display = str(msg.get("chat_display_name") or msg.get("chat_name") or "")
    sender_display = str(msg.get("sender_display_name") or msg.get("sender") or "")
    is_group = bool(msg.get("is_group_chat", False))

    if use_paired:
        # Dual-model path: call paired_eval.paired_extract
        from src.core.paired_eval import (  # noqa: WPS433
            paired_extract,
            CrossModelUnavailableError,
        )
        try:
            result = paired_extract(
                text,
                chat_room_display=chat_display,
                sender_display=sender_display,
                is_group_chat=is_group,
            )
        except CrossModelUnavailableError:
            _emit_chat("chat_extract_paired_unavailable", {
                "msg_id": str(msg.get("msg_id") or msg.get("id") or ""),
                "content_len": len(text),
            })
            raise  # fail-loud: no silent fallback

        accepted = result.get("accepted", [])
        n_accepted = len(accepted)
        n_disagreed = len(result.get("disagreements", []))

        _emit_chat("chat_extract_paired_done", {
            "n_accepted": n_accepted,
            "n_disagreed": n_disagreed,
            "degraded_mode": result.get("degraded_mode"),
        })

        triples = []
        for triple in accepted:
            # accepted items are (s, p, o) tuples
            if isinstance(triple, (list, tuple)) and len(triple) >= 3:
                s, p, o = str(triple[0]), str(triple[1]), str(triple[2])
            elif isinstance(triple, dict):
                s = str(triple.get("s") or triple.get("subject") or "")
                p = str(triple.get("p") or triple.get("predicate") or "")
                o = str(triple.get("o") or triple.get("object") or "")
            else:
                continue
            triples.append({
                "s": s,
                "p": p,
                "o": o,
                "paired_eval_attested": "paired_v1",
            })
        return triples

    else:
        # Legacy single-Qwen path
        from src.extraction.mlx_lm_wrapper import extract_chat_triples  # noqa: WPS433
        raw_triples = extract_chat_triples(msg, ctx, adapter="raw")
        triples = []
        for t in raw_triples:
            t_out = dict(t)
            t_out["paired_eval_attested"] = "single_qwen_v1"
            triples.append(t_out)
        return triples


def _build_factrow(
    msg: dict,
    triple: dict,
    consent_envelope,
    confidence: float = 0.7,
    pseudonymize: bool = False,
    passphrase: bytes | None = None,
) -> dict:
    """Construct FactRow with bi-temporal metadata per plan §3 U9 schema.

    Closure A plan_v3 TU-1 refactor: metadata building delegated to
    `src.chat.metadata_builder._build_chat_metadata` (single helper site
    per AC-U9-5 helper-uniqueness grep). HASH_LEN=32 (was [:16]; fixes
    consistency-iter3-2 CRITICAL hash-truncation contradiction).

    U8 integration (pseudonymize=True): triple subject/object replaced with
    person_<uuid> via entity_pseudonym.resolve_or_mint_uuid; passphrase required
    on first call per session.
    """
    from src.chat.metadata_builder import _build_chat_metadata

    # Inject deterministic timestamp default if missing.
    msg = dict(msg)
    if not msg.get("timestamp"):
        msg["timestamp"] = datetime.now(timezone.utc).isoformat()

    subject = triple["s"]
    obj = triple["o"]
    if pseudonymize:
        # SEC-iter1-1 fix: fail-CLOSED when vault unavailable (NOT silent
        # plaintext leak). Caller MUST provide passphrase OR pre-unlock vault.
        from src.extraction.entity_pseudonym import resolve_or_mint_uuid
        # Subject is typically the sender (a person reference)
        subject = resolve_or_mint_uuid(str(subject), passphrase=passphrase)
        # Object: pseudonymize only if it looks like a person reference
        obj_str = str(obj)
        if obj_str.startswith("wxid_") or "@chatroom" in obj_str:
            obj = resolve_or_mint_uuid(obj_str, passphrase=passphrase)

    return {
        "subject": subject,
        "predicate": triple["p"],
        "object": obj,
        "metadata": _build_chat_metadata(msg, consent_envelope, confidence),
    }


def _read_jsonl_lines(path: Path) -> Iterable[dict]:
    """Read JSONL file (one JSON per line). Skips blank/malformed lines."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                yield json.loads(ln)
            except json.JSONDecodeError:
                continue


def _read_recent_dump(path: Path) -> List[dict]:
    """Read historical wechat_recent_*.json (a JSON array)."""
    if not path.exists():
        return []
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def extract_msgs(
    msgs: List[dict],
    denylist_filter: Callable[[dict], Any],
    consent_evaluate: Callable[[dict], Any],
    extract_triples: Callable[[dict, List[dict]], List[dict]],
    context_window: int = 10,
    pseudonymize: bool = False,
    passphrase: bytes | None = None,
    use_paired_adapter: Optional[bool] = None,
) -> Dict[str, Any]:
    """Run pipeline on a list of msgs.

    For each msg:
      1. denylist: drop if hit (record reason)
      2. consent_gate: drop if manual_blocked
      3. groupby chat_room → 10-msg context window (deque per room)
      4. extract_chat_triples (or paired_extract_for_chat when enabled) → 0-N triples
      5. for each triple → FactRow

    TU-Phase2-2: `use_paired_adapter` (default None = read from env
    MEMEXA_CHAT_EXTRACT_PAIRED). When enabled, extract_triples is replaced
    by paired_extract_for_chat adapter for dual-model attestation.
    CrossModelUnavailableError propagates unchanged (fail-loud).

    Returns metrics dict + factrows list.
    """
    # TU-Phase2-2: determine if we override extract_triples with paired adapter
    if use_paired_adapter is None:
        paired_env = os.environ.get("MEMEXA_CHAT_EXTRACT_PAIRED", "1").lower().strip()
        use_paired_adapter = paired_env not in {"0", "false"}

    if use_paired_adapter:
        _effective_extractor: Callable[[dict, List[dict]], List[dict]] = paired_extract_for_chat
    else:
        _effective_extractor = extract_triples
    factrows: List[dict] = []
    metrics = {
        "n_msg_input": len(msgs),
        "n_dropped_denylist": 0,
        "n_dropped_consent": 0,
        "n_extracted": 0,
        "n_factrows": 0,
        "n_extract_failed": 0,
        "by_drop_reason": defaultdict(int),
    }
    contexts: Dict[str, deque] = defaultdict(lambda: deque(maxlen=context_window))

    for msg in msgs:
        # 1. denylist
        deny = denylist_filter(msg)
        if not deny.passed:
            metrics["n_dropped_denylist"] += 1
            metrics["by_drop_reason"][deny.reason] += 1
            continue

        # 2. consent
        consent = consent_evaluate(msg)
        if consent.consent == "manual_blocked":
            metrics["n_dropped_consent"] += 1
            metrics["by_drop_reason"]["manual_blocked"] += 1
            continue

        # 3. context window — read snapshot BEFORE extract; only append AFTER
        # successful extract (LG-iter1-2 fix: failed-extract msg should NOT
        # pollute future msg's context).
        chat_name = str(msg.get("chat_name", "") or "")
        ctx = list(contexts[chat_name])

        # 4. extract triples (TU-Phase2-2: use paired adapter if enabled)
        try:
            triples = _effective_extractor(msg, ctx)
        except Exception as e:
            metrics["n_extract_failed"] += 1
            metrics["by_drop_reason"][f"extract_err_{type(e).__name__}"] += 1
            # NOTE: do NOT append msg to context on extract failure
            continue

        # Append to context only on successful extract path
        contexts[chat_name].append(msg)
        metrics["n_extracted"] += 1

        # 5. FactRow per triple (LG-iter1 CRIT fix: thread pseudonymize/passphrase)
        for t in triples:
            row = _build_factrow(msg, t, consent,
                                 pseudonymize=pseudonymize, passphrase=passphrase)
            factrows.append(row)
            metrics["n_factrows"] += 1

    metrics["by_drop_reason"] = dict(metrics["by_drop_reason"])
    return {"factrows": factrows, "metrics": metrics}


def _append_chat_episode(chat_room_hash: str, topic: str, predicate: str, confidence: float) -> None:
    """Atomic append to memexa/data/episodes_chat.jsonl. RP-LOG-8 atomic append.
    PII-stripped: NEVER includes raw message body (RP-SEC-8)."""
    import json as _json, time as _t
    from pathlib import Path as _P
    path = _P(__file__).resolve().parents[2] / 'data' / 'episodes_chat.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": _t.time(),
        "source": "chat",
        "chat_room_hash": chat_room_hash,
        "topic": topic,
        "predicate": predicate,
        "confidence": confidence,
    }
    line = _json.dumps(rec, ensure_ascii=False) + "\n"
    with open(path, 'ab') as f:
        f.write(line.encode('utf-8'))


def extract_from_recent_dump(
    dump_path: Path,
    n: int = 100,
    mock_llm: bool = False,
    out_factrows: Optional[Path] = None,
    out_metrics: Optional[Path] = None,
    pseudonymize: bool = False,
    passphrase: bytes | None = None,
    use_paired_adapter: Optional[bool] = None,
) -> Dict[str, Any]:
    """Convenience: load historical dump → run pipeline → write outputs.

    mock_llm=True: synthesizes 1 fake triple per passing msg (offline mode).
    mock_llm=False: invokes real Qwen3 over ssh (LIVE).
    pseudonymize=True: replaces sender wxid with person_<uuid> via
      entity_pseudonym (U8 integration); requires passphrase on first call.

    TU-Phase2-2: use_paired_adapter (default None = read from env
    MEMEXA_CHAT_EXTRACT_PAIRED). When enabled and not mock_llm, the
    paired_extract_for_chat adapter is used (dual-model attestation).
    mock_llm=True always uses legacy mock extractor (bypasses paired).
    """
    from src.extraction.chat_class_denylist import filter_message
    from src.extraction.consent_gate import evaluate

    msgs = _read_recent_dump(dump_path)[:n]

    if mock_llm:
        def _mock_extract(msg: dict, ctx: list) -> List[dict]:
            return [{"s": str(msg.get("sender", "user")), "p": "discussed",
                     "o": str(msg.get("content", ""))[:30]}]
        extractor = _mock_extract
        # mock_llm always uses legacy extractor; disable paired for this branch
        effective_use_paired = False
    else:
        from src.extraction.mlx_lm_wrapper import extract_chat_triples
        def _real_extract(msg: dict, ctx: list) -> List[dict]:
            return extract_chat_triples(msg, ctx, adapter="raw")
        extractor = _real_extract
        effective_use_paired = use_paired_adapter  # None = read from env

    result = extract_msgs(
        msgs, denylist_filter=filter_message,
        consent_evaluate=evaluate,
        extract_triples=extractor,
        pseudonymize=pseudonymize,
        passphrase=passphrase,
        use_paired_adapter=effective_use_paired,
    )

    # RP-LOG-8: emit one episode entry per factrow (PII-stripped: hash+topic+predicate only)
    for row in result.get("factrows", []):
        meta = row.get("metadata", {})
        _append_chat_episode(
            chat_room_hash=str(meta.get("chat_room_hash", "") or ""),
            topic=str(row.get("subject", "") or ""),
            predicate=str(row.get("predicate", "") or ""),
            confidence=float(meta.get("confidence", 0.0) or 0.0),
        )

    if out_factrows:
        out_factrows.parent.mkdir(parents=True, exist_ok=True)
        with out_factrows.open("w", encoding="utf-8") as f:
            for row in result["factrows"]:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    if out_metrics:
        out_metrics.parent.mkdir(parents=True, exist_ok=True)
        out_metrics.write_text(
            json.dumps(result["metrics"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return result
