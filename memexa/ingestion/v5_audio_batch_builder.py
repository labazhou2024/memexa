"""V5-native audio batch builder.

Reads transcript.jsonl + clip_index.json + voice_resolution.json produced by
`memexa.extraction.audio_pipeline` and `voice_resolver.resolve_session`, then
slices the utterance stream into semantic batches and writes V5-format
prompt.json files that the existing l0_worker_api.py extractor understands.

Output layout (mirrors v5_wechat/qq/email/browser builders):
  data/l0_v5/input_batches_audio/<YYYY-MM-DD>/<batch_id>/prompt.json

Semantic batching strategy (4 boundaries, OR'd):
  1. Hard window: 15-minute ceiling (never exceed 900s of session time).
  2. Min window:  90-second floor (don't produce sub-batches below this).
  3. Silence gap: split at any inter-utterance gap >= 30s.
  4. Speaker rotation: in the absence of long silence, prefer to split when
     the active speaker set has rotated AND batch length > 5 min.
  5. Topic shift heuristic: bigram-overlap drop across a sliding 6-utterance
     window indicates a new topic — prefer split if soft conditions also hold.

Each utterance carries:
  - ts                     absolute wall-clock ISO (recording start + offset)
  - voice_canonical_id     from voice_resolution.json
  - sender_name            display_name (or voice_unknown_<hash>)
  - audio_ts_start/end     session offset seconds
  - audio_clip_uri         relative clip path for reverse-lookup
  - diariz_conf            from clustering
  - asr_avg_logprob        Whisper confidence
  - is_self                boolean (from voice_resolution)
  - content                ASR transcript

  → renders as messages[] array compatible with pass2_prompt._render_messages_audio.

batch_id = sha256(session_id + "|" + first_utt_ts)[:16]

manifest_slice strategy:
  - Default: trimmed to only persons whose voice_canonical_id or aliases
    appear in this batch's sender_list (avoids 32k token overflow, same
    reason wechat/qq/email builders strip).
  - Add public_figures/orgs/inanimate only if name appears verbatim in any
    utterance content (literal substring scan).

Where chat_room:
  - "audio:session=<session_id>" until LLM infers a better one in extraction.
  - Builder doesn't infer scene; LLM does (per pass2_prompt _SECTION_AUDIO).

Reading the session start wall-clock:
  - clip_index.json[parent_audio] -> we mtime that file as recording start.
  - If overridden via --recording-start ISO, that wins.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("v5_audio_batch_builder")

DEFAULT_OUT = ROOT / "data" / "l0_v5" / "input_batches_audio"
MANIFEST_PATH = ROOT / "data" / "identity_manifest.yaml"
SESSION_SPEAKERS_PATH = ROOT / "data" / "audio" / "session_speakers.yaml"


def load_session_config(session_id: str) -> Dict[str, Any]:
    """Look up the full session config for a session_id.

    Returns dict with `speakers` (list) and `passive_listener_session` (bool)
    and any other session-level flags. Returns {} if no match.
    """
    if not SESSION_SPEAKERS_PATH.exists():
        return {}
    try:
        import yaml
        d = yaml.safe_load(SESSION_SPEAKERS_PATH.read_text(encoding="utf-8")) or {}
        sessions = (d.get("sessions") or {})
        match = sessions.get(session_id)
        if not match:
            for key, val in sessions.items():
                if key in session_id or session_id in key:
                    match = val
                    break
        return dict(match) if isinstance(match, dict) else {}
    except Exception as exc:
        logger.warning(f"session_config load fail: {exc!r}")
        return {}


def load_session_speakers(session_id: str) -> List[Dict[str, Any]]:
    """Backward-compat: return just the speakers list."""
    cfg = load_session_config(session_id)
    return list(cfg.get("speakers") or [])

# Batching knobs (2026-05-12 v2: longer windows for academic/long-form dialog)
MAX_BATCH_SEC = 1800.0      # 30 minutes hard cap (was 15)
MIN_BATCH_SEC = 180.0       # 3 min floor — anything less merges into neighbor
SILENCE_GAP_SPLIT_SEC = 60.0   # 60s silence is a strong split signal (was 30)
SOFT_SPLIT_AFTER_SEC = 600.0   # speaker/topic soft split allowed after 10min (was 5)
TOPIC_DRIFT_BIGRAM_THRESHOLD = 0.04  # bigram overlap below this = topic shift
MAX_UTT_PER_BATCH = 400        # safety cap (was 250)

# Auto-classifier verdict per session (set by build_session_batches; consumed by build_one_batch)
_AUTO_CLASSIFICATION: Dict[str, Any] = {}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _batch_sha(session_id: str, first_ts: float) -> str:
    return hashlib.sha256(
        f"{session_id}|{int(first_ts * 1000)}".encode("utf-8")
    ).hexdigest()[:16]


def _chat_room_hash(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8", errors="replace")).hexdigest()[:32]


def _wxid_hash_from_voice(voice_canonical_id: str) -> str:
    """Stable 16-hex hash so pass2_prompt._render_sender_list can use it.

    Form: sha256("voice:" + canonical_id)[:16].
    """
    return hashlib.sha256(
        f"voice:{voice_canonical_id}".encode("utf-8")
    ).hexdigest()[:16]


def load_manifest() -> Dict[str, Any]:
    if not MANIFEST_PATH.exists():
        return {"persons": {}, "organizations": {},
                "inanimate": {}, "public_figures": {}}
    try:
        import yaml
        d = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
        return {
            "persons": d.get("persons") or {},
            "organizations": d.get("organizations") or {},
            "inanimate": d.get("inanimate") or {},
            "public_figures": d.get("public_figures") or {},
        }
    except Exception as exc:
        logger.warning(f"manifest load fail: {exc!r}")
        return {"persons": {}, "organizations": {},
                "inanimate": {}, "public_figures": {}}


def _absolute_ts(recording_start: dt.datetime, offset_s: float) -> str:
    return (recording_start + dt.timedelta(seconds=offset_s)).strftime("%Y-%m-%d %H:%M:%S")


def _bigram_set(text: str) -> set:
    chars = [c for c in (text or "") if not c.isspace()]
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}


def _bigram_overlap(a: str, b: str) -> float:
    sa = _bigram_set(a)
    sb = _bigram_set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, min(len(sa), len(sb)))


# -----------------------------------------------------------------------------
# Semantic chunker
# -----------------------------------------------------------------------------
def chunk_utterances(utts: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Slice into batches per the boundary rules above."""
    if not utts:
        return []
    batches: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    last_end = utts[0]["ts_start"]

    def commit():
        nonlocal current
        if current:
            batches.append(current)
            current = []

    for u in utts:
        start, end = float(u["ts_start"]), float(u["ts_end"])
        if not current:
            current.append(u)
            last_end = end
            continue

        gap = start - last_end
        batch_start = current[0]["ts_start"]
        batch_dur = end - batch_start

        # Rule 1 — hard window cap
        if batch_dur > MAX_BATCH_SEC:
            commit()
            current.append(u)
            last_end = end
            continue

        # Rule 2 — silence gap split
        if gap >= SILENCE_GAP_SPLIT_SEC and batch_dur >= MIN_BATCH_SEC:
            commit()
            current.append(u)
            last_end = end
            continue

        # Rule 5 — utterance count safety cap
        if len(current) >= MAX_UTT_PER_BATCH:
            commit()
            current.append(u)
            last_end = end
            continue

        # Rule 4 — semantic / topic soft split (only after SOFT threshold)
        # Trigger when EITHER (speaker rotates after long stretch) OR (topic
        # drift detected via bigram overlap drop over a window). Uses a
        # 6-utt look-back so noise from a single line doesn't fool the chunker.
        if batch_dur >= SOFT_SPLIT_AFTER_SEC:
            recent_text = " ".join(x.get("text", "") for x in current[-6:])
            ahead_text = u.get("text", "")
            drift = _bigram_overlap(recent_text, ahead_text)
            recent_spk = set(x.get("spk_local") for x in current[-5:])
            this_spk = u.get("spk_local")
            spk_rotated = bool(this_spk) and this_spk not in recent_spk

            # Topic shift evidence is strongest when BOTH drift and rotation
            # happen together; either alone after >>10min also justifies cut.
            if drift < TOPIC_DRIFT_BIGRAM_THRESHOLD and (
                spk_rotated or batch_dur > MAX_BATCH_SEC * 0.7
            ):
                commit()
                current.append(u)
                last_end = end
                continue

        current.append(u)
        last_end = end

    commit()
    # Ensure last batch isn't a tiny tail < MIN_BATCH_SEC by merging up
    if len(batches) >= 2:
        last = batches[-1]
        last_dur = last[-1]["ts_end"] - last[0]["ts_start"]
        if last_dur < MIN_BATCH_SEC:
            batches[-2].extend(last)
            batches.pop()
    return batches


# -----------------------------------------------------------------------------
# Builder per batch
# -----------------------------------------------------------------------------
def build_one_batch(
    batch_utts: List[Dict[str, Any]],
    session_id: str,
    recording_start: dt.datetime,
    voice_resolution: Dict[str, Dict[str, Any]],
    manifest: Dict[str, Any],
    parent_audio: str,
    output_dir: Path,
) -> Tuple[str, Path]:
    """Materialize one batch into a prompt.json directory.

    Returns (batch_id, path_to_prompt_json).
    """
    if not batch_utts:
        raise ValueError("empty batch")

    first_ts = batch_utts[0]["ts_start"]
    last_ts_end = batch_utts[-1]["ts_end"]
    batch_id = _batch_sha(session_id, first_ts)

    # batch date dir from absolute wall-clock of first utt
    abs_start = recording_start + dt.timedelta(seconds=first_ts)
    abs_end = recording_start + dt.timedelta(seconds=last_ts_end)
    date_dir = abs_start.strftime("%Y-%m-%d")
    bdir = output_dir / date_dir / batch_id
    bdir.mkdir(parents=True, exist_ok=True)

    # Build sender_list
    seen_spk: Dict[str, Dict[str, Any]] = {}
    for u in batch_utts:
        spk = u.get("spk_local") or "spk_?"
        if spk in seen_spk:
            continue
        res = voice_resolution.get(spk, {})
        canonical_id = res.get("canonical_id") or f"voice_unknown_{spk}"
        display = res.get("display_name") or canonical_id
        wh = _wxid_hash_from_voice(canonical_id)
        seen_spk[spk] = {
            "wxid_hash": wh,
            "sender_name": display,
            "alias_in_manifest_or_None": None,
            "is_self": bool(res.get("is_self", False)),
            "voice_canonical_id": canonical_id,
            "spk_local": spk,
            "match_status": res.get("match_status", "unknown_new"),
            "match_sim": res.get("match_sim", 0.0),
        }
    sender_list = list(seen_spk.values())

    # Build messages
    messages: List[Dict[str, Any]] = []
    full_text_blob: List[str] = []
    for u in batch_utts:
        spk = u.get("spk_local") or "spk_?"
        meta = seen_spk[spk]
        offset_start = float(u["ts_start"])
        offset_end = float(u["ts_end"])
        ts_iso = _absolute_ts(recording_start, offset_start)
        text = (u.get("text") or "").strip()
        if not text:
            continue
        messages.append({
            "ts": ts_iso,
            "wxid_hash": meta["wxid_hash"],
            "voice_canonical_id": meta["voice_canonical_id"],
            "sender_name": meta["sender_name"],
            "content": text,
            "audio_ts_start": offset_start,
            "audio_ts_end": offset_end,
            "audio_clip_uri": u.get("clip_uri", ""),
            "diariz_conf": float(u.get("diariz_conf", 0.0)),
            "asr_avg_logprob": float(u.get("avg_logprob", 0.0)),
            "asr_no_speech_prob": float(u.get("no_speech_prob", 0.0)),
            "language": u.get("language", "?"),
        })
        full_text_blob.append(text)

    # Manifest slice — only entries whose name/alias appears in content
    blob = " ".join(full_text_blob)

    def _str_only(items: Any) -> List[str]:
        """Coerce a heterogeneous list (may contain dicts/None/non-str) → list[str]."""
        if not items:
            return []
        out: List[str] = []
        for v in items:
            if isinstance(v, str) and v:
                out.append(v)
            elif isinstance(v, dict):
                for k in ("name", "value", "text", "aka"):
                    vv = v.get(k)
                    if isinstance(vv, str) and vv:
                        out.append(vv)
        return out

    def _entry_name_hits(entry: Any) -> bool:
        if not isinstance(entry, dict):
            return False
        primary = entry.get("primary_name") or ""
        names = ([primary] if isinstance(primary, str) and primary else []) + _str_only(entry.get("aka"))
        # Skip names too short (≤1 char) to avoid noise like "." matching any period
        return any(len(n) >= 2 and n in blob for n in names)

    persons_slice: Dict[str, Any] = {}
    for cid, p in manifest.get("persons", {}).items():
        if _entry_name_hits(p):
            persons_slice[cid] = p
    # Always include self if known in voice_manifest → person link
    for s in sender_list:
        if s.get("is_self"):
            pcid = (voice_resolution.get(s["spk_local"], {})
                    .get("person_canonical_id"))
            if pcid and pcid in manifest.get("persons", {}):
                persons_slice.setdefault(pcid, manifest["persons"][pcid])
    orgs_slice: Dict[str, Any] = {
        cid: o for cid, o in manifest.get("organizations", {}).items()
        if _entry_name_hits(o)
    }
    inanimate_slice: Dict[str, Any] = {
        cid: it for cid, it in manifest.get("inanimate", {}).items()
        if _entry_name_hits(it)
    }
    pubfig_slice: Dict[str, Any] = {
        cid: p for cid, p in manifest.get("public_figures", {}).items()
        if _entry_name_hits(p)
    }
    manifest_slice = {
        "persons": persons_slice,
        "organizations": orgs_slice,
        "inanimate": inanimate_slice,
        "public_figures": pubfig_slice,
    }

    # Look up declared speakers + session-level flags
    session_cfg = load_session_config(session_id)
    known_speakers = list(session_cfg.get("speakers") or [])
    passive_listener = bool(session_cfg.get("passive_listener_session", False))
    # If no explicit config, fall back to auto-classifier verdict (set by
    # build_session_batches above).
    auto_cls = _AUTO_CLASSIFICATION.get(session_id) if "_AUTO_CLASSIFICATION" in globals() else None
    if not session_cfg and auto_cls:
        if auto_cls.get("verdict") in ("passive", "unknown"):
            # Conservative: treat unknown as passive too (no auto-attribution)
            passive_listener = True
            logger.info(f"  {batch_id}: auto-classified passive (no yaml config)")

    # Top-level prompt.json schema (matches what l0_worker_api.collect_pending
    # expects + pass2_prompt.build_pass2_user_prompt consumes)
    chat_room = f"audio:session={session_id}"
    prompt = {
        "batch_id": batch_id,
        "source": "audio",
        "source_kind": "audio",
        "chat_room": chat_room,
        "room_hash": _chat_room_hash(chat_room),
        "room_tier": 1,                # default 1; LLM may upgrade
        "batch_window_local": (
            f"{abs_start.strftime('%Y-%m-%d %H:%M:%S')} - "
            f"{abs_end.strftime('%Y-%m-%d %H:%M:%S')}"
        ),
        "session_id": session_id,
        "parent_audio": parent_audio,
        "audio_offset_window": [float(first_ts), float(last_ts_end)],
        "sender_list": sender_list,
        "manifest_slice": manifest_slice,
        "messages": messages,
        # When the session has pre-declared participants (CEO knew them in
        # advance), pass through so pass2_prompt LLM knows to consolidate
        # voice_unknown_* into these known canonical_ids.
        "known_speakers": known_speakers,
        # passive_listener_session=true → CEO is audience, not the dominant
        # speaker. LLM should NOT auto-attribute teacher's "我" to is_self.
        "passive_listener_session": passive_listener,
    }

    (bdir / "prompt.json").write_text(
        json.dumps(prompt, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return batch_id, bdir / "prompt.json"


# -----------------------------------------------------------------------------
# Top-level builder for a session dir
# -----------------------------------------------------------------------------
def _auto_classify_session_dir(session_dir: Path,
                                recording_start: Optional[dt.datetime]
                                ) -> Optional[Dict[str, Any]]:
    """Run audio_session_classifier on a session_dir's transcript."""
    try:
        from memexa.extraction.audio_session_classifier import classify_session
        return classify_session(
            transcript_path=session_dir / "transcript.jsonl",
            recording_start=recording_start,
        )
    except Exception as exc:
        logger.warning(f"auto_classify fail: {exc!r}")
        return None


def build_session_batches(
    session_dir: Path,
    output_dir: Path = DEFAULT_OUT,
    recording_start: Optional[dt.datetime] = None,
    voice_manifest_path: Optional[Path] = None,
    threshold: float = 0.55,
    skip_existing: bool = True,
) -> List[str]:
    """Read session_dir/transcript.jsonl + clip_index.json, resolve voices,
    semantic-chunk, and write batches.

    Returns list of batch_ids produced.
    """
    transcript = session_dir / "transcript.jsonl"
    clip_index_p = session_dir / "clip_index.json"
    if not transcript.exists():
        raise FileNotFoundError(transcript)

    # Safeguard: 禁止合成测试 session_id 直接写入真实 input_batches_audio
    # (避免 calendar_daemon 把虚构事件转为真实日历)
    # 2026-05-12 incident: audio_meaningful_synth + audio_synthtest writes
    # produced fake "去誉讯门店下单 Mac Studio" + "准备 slides" calendar events.
    _SYNTH_MARKERS = ("synth", "fake", "demo")
    try:
        out_resolved = output_dir.resolve()
        is_real_path = out_resolved == DEFAULT_OUT.resolve()
    except Exception:
        is_real_path = False
    sid_str = session_dir.name.lower()
    if is_real_path and any(m in sid_str for m in _SYNTH_MARKERS):
        raise ValueError(
            f"REFUSE: session_dir name {session_dir.name!r} looks synthetic "
            f"(matched markers {_SYNTH_MARKERS}); refusing to write to real "
            f"{DEFAULT_OUT}. Use a tempdir output for tests."
        )

    utts: List[Dict[str, Any]] = []
    with transcript.open(encoding="utf-8") as fp:
        for line in fp:
            utts.append(json.loads(line))
    utts.sort(key=lambda u: u["ts_start"])

    clip_index = (json.loads(clip_index_p.read_text(encoding="utf-8"))
                  if clip_index_p.exists() else {})
    session_id = clip_index.get("session_id") or session_dir.name
    parent_audio = clip_index.get("parent_audio", "")

    # Voice resolution
    from memexa.extraction.voice_resolver import (
        load_manifest as load_vm, session_cluster_centroids, resolve_session,
    )
    voices = load_vm(voice_manifest_path) if voice_manifest_path else load_vm()
    centroids = session_cluster_centroids(utts)
    voice_resolution = resolve_session(centroids, voices, threshold=threshold)

    # Persist resolution for inspection
    (session_dir / "voice_resolution.json").write_text(
        json.dumps(voice_resolution, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Recording start = recording_end - duration. recording_end derivable from
    # (a) explicit CLI flag, (b) Mac-side mtime of parent_audio via SSH (Win
    # cannot stat Mac paths), (c) local stat if path is local. Falls back to
    # now() — but warns since this produces wrong absolute timestamps.
    if recording_start is None:
        end_ts = None
        # Compute total duration: last utt's ts_end + buffer is more reliable
        # than the file mtime since trailing silence after talk may be
        # part of the recording.
        total_dur_s = float(utts[-1]["ts_end"]) if utts else 0.0

        local_p = Path(parent_audio) if parent_audio else None
        if local_p and local_p.exists():
            mtime = local_p.stat().st_mtime
            end_ts = dt.datetime.fromtimestamp(mtime).astimezone()
        elif parent_audio:
            # Try SSH stat to Mac to get the recording-end mtime
            try:
                import subprocess
                ssh_alias = "primary-host"
                cmd = ["ssh", ssh_alias, "stat", "-f", "%m", parent_audio]
                r = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=10, encoding="utf-8", errors="replace")
                if r.returncode == 0 and r.stdout.strip().isdigit():
                    epoch = int(r.stdout.strip())
                    end_ts = dt.datetime.fromtimestamp(epoch).astimezone()
                    logger.info(f"recording_end from Mac mtime: {end_ts.isoformat()}")
            except Exception as exc:
                logger.warning(f"ssh stat mtime fail: {exc!r}")

        if end_ts is not None:
            recording_start = end_ts - dt.timedelta(seconds=total_dur_s)
            logger.info(f"derived recording_start={recording_start.isoformat()} "
                        f"(end={end_ts.isoformat()}, dur={total_dur_s:.0f}s)")
        else:
            recording_start = dt.datetime.now().astimezone()
            logger.warning(
                f"recording_start fallback=now() — absolute timestamps WILL BE WRONG. "
                f"Pass --recording-start explicitly to fix."
            )

    manifest = load_manifest()

    # Auto-classify session if no explicit config (cron robustness)
    session_cfg_global = load_session_config(session_id)
    if not session_cfg_global:
        cls = _auto_classify_session_dir(session_dir, recording_start)
        if cls:
            logger.info(f"auto_classify {session_id}: verdict={cls.get('verdict')} "
                        f"reason={cls.get('reason')}")
            # Inject into session config used downstream
            # by mutating session_speakers cache via a wrapper.
            # Simpler: stash classification in a module-level dict the
            # build_one_batch step reads.
            global _AUTO_CLASSIFICATION
            _AUTO_CLASSIFICATION = {session_id: cls}
        else:
            _AUTO_CLASSIFICATION = {}
    else:
        _AUTO_CLASSIFICATION = {}

    # Chunk + build
    chunks = chunk_utterances(utts)
    logger.info(f"session {session_id}: {len(utts)} utts → {len(chunks)} batches")

    batch_ids: List[str] = []
    for batch_utts in chunks:
        try:
            bid, ppath = build_one_batch(
                batch_utts, session_id, recording_start,
                voice_resolution, manifest, parent_audio, output_dir,
            )
            batch_ids.append(bid)
            logger.info(f"  built batch {bid} : "
                        f"{len(batch_utts)} utts, "
                        f"{batch_utts[-1]['ts_end'] - batch_utts[0]['ts_start']:.0f}s "
                        f"→ {ppath}")
        except Exception as exc:
            logger.exception(f"build_one_batch fail: {exc!r}")

    return batch_ids


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="v5 audio batch builder")
    p.add_argument("--session-dir", required=True, type=Path,
                   help="Output dir of audio_pipeline.py (contains transcript.jsonl)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--threshold", type=float, default=0.55)
    p.add_argument("--recording-start", default=None,
                   help="ISO start time; default = parent_audio mtime")
    p.add_argument("--voice-manifest", type=Path, default=None)
    p.add_argument("--skip-existing", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    rec_start = (dt.datetime.fromisoformat(args.recording_start).astimezone()
                 if args.recording_start else None)
    batch_ids = build_session_batches(
        args.session_dir, args.out, rec_start,
        args.voice_manifest, args.threshold, args.skip_existing,
    )
    print(json.dumps({"session_dir": str(args.session_dir),
                      "batch_ids": batch_ids,
                      "n_batches": len(batch_ids)}, indent=2))


if __name__ == "__main__":
    main()
