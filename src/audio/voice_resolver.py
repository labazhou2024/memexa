"""Voice canonical-id resolver for audio source.

Maps local cluster ids (spk_0, spk_1, ...) produced by audio_pipeline.py
to global voice_canonical_id values backed by `data/audio/voice_manifest.json`.

Two roles:
  1. Per-session resolve: given a list of utterances with local spk labels +
     192d ECAPA embeddings, decide which cluster matches an enrolled voice
     (cosine sim to manifest centroids), else allocate voice_unknown_<hash>.
  2. Enroll: add a new voice (canonical_id + display_name + sample embeddings)
     into the manifest. Idempotent re-enroll merges embedding sets.

Design:
  - Manifest is JSON, human-editable.
  - Each entry stores up to 32 embeddings (cycled FIFO) plus a centroid
    that's recomputed on every update.
  - Match decision uses cosine sim to centroid; threshold = 0.55 default
    (configurable). Above → match; below → unknown.
  - voice_unknown_<8hex> id is a stable hash of the in-session centroid,
    so the same unknown voice across sessions gets the same id until
    a CEO enrolls it as a known canonical_id.

Cross-source link: a voice_canonical_id of the form `voice_<canonical_name>`
is treated as the same person as the wechat/email person of that name when
identity_resolver merges. The manifest entry has an optional
`person_canonical_id` field for explicit linking when names differ.

NOT used at session-process time directly — Stage 5 (batch builder) imports
this to render messages with proper voice_canonical_id.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("voice_resolver")

DEFAULT_MANIFEST = (
    Path(__file__).resolve().parents[2] / "data" / "audio" / "voice_manifest.json"
)
DEFAULT_THRESHOLD = 0.55       # cosine sim above this → match
MAX_EMBEDS_PER_VOICE = 32      # FIFO buffer per enrolled voice


# -----------------------------------------------------------------------------
# Data model
# -----------------------------------------------------------------------------
@dataclass
class VoiceEntry:
    canonical_id: str          # e.g. "voice_Alice"
    display_name: str          # human-readable (Chinese OK)
    is_self: bool = False
    person_canonical_id: Optional[str] = None  # link to identity_manifest persons
    embeds: List[List[float]] = field(default_factory=list)  # up to 32 192d
    centroid: List[float] = field(default_factory=list)      # 192d, L2 normed
    notes: str = ""
    enrolled_at: str = ""      # ISO timestamp


# -----------------------------------------------------------------------------
# Manifest IO
# -----------------------------------------------------------------------------
def load_manifest(path: Path = DEFAULT_MANIFEST) -> Dict[str, VoiceEntry]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: Dict[str, VoiceEntry] = {}
    for cid, d in (raw.get("voices") or {}).items():
        out[cid] = VoiceEntry(
            canonical_id=cid,
            display_name=d.get("display_name", cid),
            is_self=bool(d.get("is_self", False)),
            person_canonical_id=d.get("person_canonical_id"),
            embeds=d.get("embeds") or [],
            centroid=d.get("centroid") or [],
            notes=d.get("notes", ""),
            enrolled_at=d.get("enrolled_at", ""),
        )
    return out


def save_manifest(voices: Dict[str, VoiceEntry],
                  path: Path = DEFAULT_MANIFEST) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "threshold_default": DEFAULT_THRESHOLD,
        "max_embeds_per_voice": MAX_EMBEDS_PER_VOICE,
        "voices": {
            v.canonical_id: {
                "display_name": v.display_name,
                "is_self": v.is_self,
                "person_canonical_id": v.person_canonical_id,
                "embeds": v.embeds,
                "centroid": v.centroid,
                "notes": v.notes,
                "enrolled_at": v.enrolled_at,
            }
            for v in voices.values()
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


# -----------------------------------------------------------------------------
# Cosine math (avoid numpy dep here)
# -----------------------------------------------------------------------------
def _norm(v: List[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def _normalize(v: List[float]) -> List[float]:
    n = _norm(v)
    if n < 1e-9:
        return [0.0] * len(v)
    return [x / n for x in v]


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    return sum(x * y for x, y in zip(a, b))  # assume both already L2-normalized


def _mean(vecs: List[List[float]]) -> List[float]:
    if not vecs:
        return []
    n = len(vecs)
    dim = len(vecs[0])
    return [sum(v[i] for v in vecs) / n for i in range(dim)]


def _recompute_centroid(entry: VoiceEntry) -> None:
    if not entry.embeds:
        entry.centroid = []
        return
    entry.centroid = _normalize(_mean(entry.embeds))


# -----------------------------------------------------------------------------
# Unknown-id allocation
# -----------------------------------------------------------------------------
def stable_unknown_id(centroid: List[float]) -> str:
    """Hash the (rounded) centroid so the same unknown voice gets the same
    voice_unknown_<8hex> across sessions until enrolled."""
    rounded = [round(x, 3) for x in centroid]
    h = hashlib.sha256(json.dumps(rounded).encode("utf-8")).hexdigest()[:8]
    return f"voice_unknown_{h}"


# -----------------------------------------------------------------------------
# Resolve a session's local clusters to global ids
# -----------------------------------------------------------------------------
def resolve_session(
    cluster_centroids: Dict[str, List[float]],  # spk_local -> centroid
    voices: Dict[str, VoiceEntry],
    threshold: float = DEFAULT_THRESHOLD,
) -> Dict[str, Dict[str, Any]]:
    """For each spk_local, decide which canonical_id wins.

    Returns: {spk_local: {canonical_id, match_sim, match_status, display_name}}
      match_status ∈ {"enrolled_match", "unknown_new", "unknown_stable"}
    """
    out: Dict[str, Dict[str, Any]] = {}
    for spk_local, cent in cluster_centroids.items():
        cent = _normalize(cent)
        best_id: Optional[str] = None
        best_sim = -1.0
        for v in voices.values():
            if not v.centroid:
                continue
            sim = _cosine(cent, v.centroid)
            if sim > best_sim:
                best_sim = sim
                best_id = v.canonical_id
        if best_id is not None and best_sim >= threshold:
            out[spk_local] = {
                "canonical_id": best_id,
                "match_sim": best_sim,
                "match_status": "enrolled_match",
                "display_name": voices[best_id].display_name,
                "is_self": voices[best_id].is_self,
                "person_canonical_id": voices[best_id].person_canonical_id,
            }
        else:
            uid = stable_unknown_id(cent)
            out[spk_local] = {
                "canonical_id": uid,
                "match_sim": best_sim,
                "match_status": "unknown_new" if best_sim < 0 else "unknown_stable",
                "display_name": uid,
                "is_self": False,
                "person_canonical_id": None,
            }
    return out


def session_cluster_centroids(utterances: List[Dict[str, Any]]
                              ) -> Dict[str, List[float]]:
    """Build per-cluster centroid from a list of utterance dicts (with
    voice_embed + spk_local fields, e.g. parsed from transcript.jsonl)."""
    buckets: Dict[str, List[List[float]]] = {}
    for u in utterances:
        spk = u.get("spk_local")
        emb = u.get("voice_embed")
        if not spk or not emb or all(abs(x) < 1e-6 for x in emb):
            continue
        buckets.setdefault(spk, []).append(emb)
    return {spk: _normalize(_mean(embs)) for spk, embs in buckets.items()}


# -----------------------------------------------------------------------------
# Enroll API
# -----------------------------------------------------------------------------
def enroll_voice(
    canonical_id: str,
    display_name: str,
    sample_embeds: List[List[float]],
    *,
    is_self: bool = False,
    person_canonical_id: Optional[str] = None,
    notes: str = "",
    path: Path = DEFAULT_MANIFEST,
) -> VoiceEntry:
    """Enroll a new voice (or merge embeddings into existing)."""
    from datetime import datetime, timezone

    voices = load_manifest(path)
    entry = voices.get(canonical_id) or VoiceEntry(
        canonical_id=canonical_id,
        display_name=display_name,
        is_self=is_self,
        person_canonical_id=person_canonical_id,
        embeds=[],
        centroid=[],
        notes=notes,
        enrolled_at=datetime.now(timezone.utc).isoformat(),
    )
    # Merge embeds (FIFO cap)
    new_embs = [_normalize(e) for e in sample_embeds if _norm(e) > 1e-6]
    entry.embeds = (entry.embeds + new_embs)[-MAX_EMBEDS_PER_VOICE:]
    _recompute_centroid(entry)
    if not entry.display_name:
        entry.display_name = display_name
    voices[canonical_id] = entry
    save_manifest(voices, path)
    logger.info(f"enrolled {canonical_id}: {len(entry.embeds)} embeds total")
    return entry


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def _cli_enroll(args):
    """Enroll from a transcript.jsonl by specifying which spk_local belongs
    to which canonical_id."""
    voices = load_manifest(args.manifest)
    embs: List[List[float]] = []
    with open(args.transcript, encoding="utf-8") as fp:
        for line in fp:
            u = json.loads(line)
            if u.get("spk_local") != args.spk_local:
                continue
            if u.get("duration", 0) < 1.0:
                continue
            embs.append(u["voice_embed"])
    if not embs:
        print(f"no utterances found for spk={args.spk_local}")
        return
    enroll_voice(
        canonical_id=args.canonical_id,
        display_name=args.display_name,
        sample_embeds=embs[:MAX_EMBEDS_PER_VOICE],
        is_self=args.is_self,
        person_canonical_id=args.person_canonical_id,
        notes=args.notes or "",
        path=args.manifest,
    )
    print(f"enrolled {args.canonical_id} with {len(embs)} samples")


def _cli_resolve(args):
    """Resolve a transcript.jsonl's local spk labels against the manifest."""
    voices = load_manifest(args.manifest)
    utts: List[Dict[str, Any]] = []
    with open(args.transcript, encoding="utf-8") as fp:
        for line in fp:
            utts.append(json.loads(line))
    centroids = session_cluster_centroids(utts)
    res = resolve_session(centroids, voices, threshold=args.threshold)
    out_path = Path(args.transcript).with_name("voice_resolution.json")
    out_path.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print(f"\nwrote {out_path}")


def _cli_list(args):
    voices = load_manifest(args.manifest)
    print(f"# {len(voices)} voices in {args.manifest}\n")
    for v in voices.values():
        flag = "[SELF]" if v.is_self else ""
        link = f"-> {v.person_canonical_id}" if v.person_canonical_id else ""
        print(f"  {v.canonical_id}  display={v.display_name!r}  "
              f"{flag} {link}  embeds={len(v.embeds)}")


def main():
    import argparse
    p = argparse.ArgumentParser(description="voice_resolver: manifest + resolve")
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enroll", help="enroll a voice from transcript.jsonl")
    e.add_argument("--transcript", required=True)
    e.add_argument("--spk-local", required=True, dest="spk_local")
    e.add_argument("--canonical-id", required=True, dest="canonical_id")
    e.add_argument("--display-name", required=True, dest="display_name")
    e.add_argument("--is-self", action="store_true", dest="is_self")
    e.add_argument("--person-canonical-id", default=None,
                   dest="person_canonical_id")
    e.add_argument("--notes", default="")
    e.set_defaults(func=_cli_enroll)

    r = sub.add_parser("resolve", help="resolve spk_local -> canonical_id")
    r.add_argument("--transcript", required=True)
    r.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    r.set_defaults(func=_cli_resolve)

    l = sub.add_parser("list", help="list enrolled voices")
    l.set_defaults(func=_cli_list)

    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
