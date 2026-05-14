"""Audio ASR + speaker diarization pipeline (Stage 1-4 of audio source).

Input  : path to an audio file (m4a / wav / mp3 / flac / opus / ...)
Output : transcript.jsonl  (one utterance per line)
         clean.opus         (VAD-filtered, lossy compressed copy for L1 storage)
         clip_index.json    (utt_id -> {wav_offset_s, dur_s, opus_byte_range})
         pipeline_stats.json (timing + counts for monitoring)

Stages:
  1. ffmpeg decode      → 16 kHz mono int16 numpy array (in-memory)
  2. Silero VAD         → speech segments [start_s, end_s]
  3. mlx-whisper large-v3 → segment-level transcript + word ts + avg_logprob
  4. SpeechBrain ECAPA-TDNN → 192d voice embedding per utterance
  5. Sklearn AgglomerativeClustering (cosine) → speaker label per utterance

Speaker IDs at this stage are local (spk_0, spk_1, ...) — final
voice_canonical_id mapping happens in voice_resolver.py against
data/audio/voice_manifest.json.

No HuggingFace token needed; all models are public:
  - mlx-community/whisper-large-v3-mlx
  - speechbrain/spkrec-ecapa-voxceleb
  - snakers4/silero-vad (PyPI silero-vad pkg)

OMP conflict workaround (librosa + torch + mlx all link libomp):
  export KMP_DUPLICATE_LIB_OK=TRUE
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Suppress OMP duplicate-lib error before any audio import
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# Critical: prevent mlx-whisper from HF-checking the model on each
# transcribe() call. With HF_HUB_OFFLINE=1 the model loads once from local
# cache and subsequent inference is pure local. Without this, per-segment
# calls hang on CLOUDFRONT in CLOSE_WAIT (LIVE 2026-05-12, 30+ min stall
# for 1526 segments at network-stalled rate).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

logger = logging.getLogger("audio_pipeline")

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
TARGET_SR = 16_000           # Whisper / Silero / ECAPA all expect 16 kHz mono
WHISPER_MODEL = "mlx-community/whisper-large-v3-mlx"
ECAPA_MODEL = "speechbrain/spkrec-ecapa-voxceleb"
VAD_MIN_SPEECH_MS = 250      # drop segments shorter than this
VAD_MIN_SILENCE_MS = 500     # gaps < this don't split segments
VAD_SPEECH_PAD_MS = 100      # pad each segment by this on each side
ASR_LANGUAGE = None          # None = auto detect; "zh"/"en" force
ASR_CHUNK_S = 30.0           # whisper input window
ECAPA_MIN_DUR_S = 0.5        # below this, embedding is unreliable (set conf=0)
CLUSTER_COSINE_THRESHOLD = 0.4   # smaller = more speakers; ~0.4 typical
MAX_SPEAKERS = 12            # safety cap


# -----------------------------------------------------------------------------
# Data structures
# -----------------------------------------------------------------------------
@dataclass
class Utterance:
    utt_id: str                  # f"{session_id}_{idx:04d}"
    idx: int
    ts_start: float              # seconds offset in session
    ts_end: float
    duration: float
    text: str                    # ASR transcript (may include errors)
    avg_logprob: float           # Whisper avg log-probability over tokens
    no_speech_prob: float        # Whisper VAD-style hallucination check
    language: str                # e.g. "zh"
    spk_local: str               # "spk_0" / "spk_1" (pre-resolution)
    diariz_conf: float           # clustering confidence in [0,1]
    voice_embed: List[float]     # 192d ECAPA embedding
    words: List[Dict[str, Any]] = field(default_factory=list)  # [{w,start,end,prob}]
    clip_uri: str = ""           # relative path to opus clip (filled later)


@dataclass
class PipelineResult:
    session_id: str
    audio_input: str
    duration_total: float        # full audio duration s
    duration_speech: float       # VAD-active duration s
    speech_ratio: float
    n_utterances: int
    n_speakers: int
    language_distribution: Dict[str, int]
    timing: Dict[str, float]     # stage -> seconds
    output_dir: str


# -----------------------------------------------------------------------------
# Stage 1 — ffmpeg decode
# -----------------------------------------------------------------------------
def _ffmpeg_bin() -> str:
    """Return ffmpeg binary (prefer imageio-ffmpeg bundled, fall back to PATH)."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def decode_to_pcm16k(input_path: Path) -> Tuple["np.ndarray", float]:
    """Decode arbitrary audio to 16 kHz mono float32 numpy array.

    Returns (samples, duration_seconds).
    """
    import numpy as np  # local import keeps optional deps optional
    import subprocess

    t0 = time.time()
    ffmpeg = _ffmpeg_bin()
    cmd = [
        ffmpeg, "-loglevel", "error", "-y",
        "-i", str(input_path),
        "-ac", "1", "-ar", str(TARGET_SR),
        "-f", "s16le", "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    duration = len(pcm) / TARGET_SR
    logger.info(f"decode: {duration:.1f}s @ {TARGET_SR}Hz mono "
                f"in {time.time() - t0:.1f}s")
    return pcm, duration


# -----------------------------------------------------------------------------
# Stage 2 — Silero VAD
# -----------------------------------------------------------------------------
def run_vad(samples: "np.ndarray") -> List[Tuple[float, float]]:
    """Return list of (start_s, end_s) for each speech segment."""
    import torch
    from silero_vad import load_silero_vad, get_speech_timestamps

    t0 = time.time()
    model = load_silero_vad()
    speech_ts = get_speech_timestamps(
        torch.from_numpy(samples),
        model,
        sampling_rate=TARGET_SR,
        min_speech_duration_ms=VAD_MIN_SPEECH_MS,
        min_silence_duration_ms=VAD_MIN_SILENCE_MS,
        speech_pad_ms=VAD_SPEECH_PAD_MS,
        return_seconds=True,
    )
    segs = [(s["start"], s["end"]) for s in speech_ts]
    logger.info(f"vad: {len(segs)} segments, "
                f"{sum(e - s for s, e in segs):.1f}s speech "
                f"in {time.time() - t0:.1f}s")
    return segs


# -----------------------------------------------------------------------------
# Stage 3 — Whisper ASR (mlx)
# -----------------------------------------------------------------------------
def run_asr(samples: "np.ndarray",
            segments: List[Tuple[float, float]]) -> List[Dict[str, Any]]:
    """Run Whisper on each segment. Returns list aligned with `segments`.

    Each item: {text, avg_logprob, no_speech_prob, language, words}
    """
    import numpy as np
    import mlx_whisper

    t0 = time.time()
    results: List[Dict[str, Any]] = []
    for i, (s, e) in enumerate(segments):
        s_idx, e_idx = int(s * TARGET_SR), int(e * TARGET_SR)
        clip = samples[s_idx:e_idx]
        if len(clip) < int(0.3 * TARGET_SR):
            results.append({"text": "", "avg_logprob": -10.0,
                            "no_speech_prob": 1.0, "language": "?",
                            "words": []})
            continue
        try:
            out = mlx_whisper.transcribe(
                clip,
                path_or_hf_repo=WHISPER_MODEL,
                language=ASR_LANGUAGE,
                word_timestamps=True,
                condition_on_previous_text=False,
                temperature=0.0,
                no_speech_threshold=0.6,
            )
        except Exception as exc:
            logger.warning(f"whisper seg #{i} fail: {exc!r}")
            results.append({"text": "", "avg_logprob": -10.0,
                            "no_speech_prob": 1.0, "language": "?",
                            "words": []})
            continue

        text = (out.get("text") or "").strip()
        lang = out.get("language") or "?"

        # Aggregate per-segment fields (Whisper returns one or more sub-segments)
        sub = out.get("segments") or []
        if sub:
            avg_lp = sum(x.get("avg_logprob", 0.0) for x in sub) / len(sub)
            no_sp = sum(x.get("no_speech_prob", 0.0) for x in sub) / len(sub)
        else:
            avg_lp = 0.0
            no_sp = 0.0

        # Collect word timestamps, shift by segment start
        words: List[Dict[str, Any]] = []
        for ss in sub:
            for w in ss.get("words") or []:
                words.append({
                    "w": w.get("word", "").strip(),
                    "start": s + float(w.get("start", 0.0)),
                    "end": s + float(w.get("end", 0.0)),
                    "prob": float(w.get("probability", 0.0)),
                })

        results.append({
            "text": text,
            "avg_logprob": float(avg_lp),
            "no_speech_prob": float(no_sp),
            "language": lang,
            "words": words,
        })

    logger.info(f"asr: {len(segments)} segs in {time.time() - t0:.1f}s")
    return results


# -----------------------------------------------------------------------------
# Stage 4 — ECAPA-TDNN voice embeddings
# -----------------------------------------------------------------------------
class _EcapaSingleton:
    _model = None

    @classmethod
    def get(cls):
        if cls._model is None:
            from speechbrain.inference import EncoderClassifier
            cls._model = EncoderClassifier.from_hparams(
                source=ECAPA_MODEL,
                run_opts={"device": "cpu"},  # MPS sometimes unstable for ECAPA
                savedir=str(Path.home() / "MEMEXA_audio" / "models" / "ecapa"),
            )
        return cls._model


def run_embeddings(samples: "np.ndarray",
                   segments: List[Tuple[float, float]]) -> List[List[float]]:
    """Compute 192d embedding for each segment. Below-min-duration → zeros."""
    import numpy as np
    import torch

    t0 = time.time()
    model = _EcapaSingleton.get()
    embeds: List[List[float]] = []
    for (s, e) in segments:
        s_idx, e_idx = int(s * TARGET_SR), int(e * TARGET_SR)
        clip = samples[s_idx:e_idx]
        if (e - s) < ECAPA_MIN_DUR_S or len(clip) == 0:
            embeds.append([0.0] * 192)
            continue
        try:
            tensor = torch.from_numpy(clip).unsqueeze(0)
            emb = model.encode_batch(tensor).squeeze().detach().cpu().numpy()
            # encode_batch returns shape (1, 1, 192); reduce to (192,)
            emb = np.asarray(emb).reshape(-1).astype(np.float32)
            # L2 normalize for cosine
            n = float(np.linalg.norm(emb))
            if n > 0:
                emb = emb / n
            embeds.append(emb.tolist())
        except Exception as exc:
            logger.warning(f"ecapa seg fail: {exc!r}")
            embeds.append([0.0] * 192)
    logger.info(f"ecapa: {len(segments)} embeddings in {time.time() - t0:.1f}s")
    return embeds


# -----------------------------------------------------------------------------
# Stage 5 — Speaker clustering (Agglomerative cosine)
# -----------------------------------------------------------------------------
def cluster_speakers(embeds: List[List[float]],
                     threshold: float = CLUSTER_COSINE_THRESHOLD,
                     max_speakers: int = MAX_SPEAKERS
                     ) -> Tuple[List[str], List[float]]:
    """Return (speaker_labels, confidences).

    confidence = 1 - (mean cosine distance to own cluster centroid) clipped to [0,1].
    Items with zero embeddings → spk_? conf 0.0.
    """
    import numpy as np

    t0 = time.time()
    n = len(embeds)
    if n == 0:
        return [], []

    arr = np.asarray(embeds, dtype=np.float32)
    valid_mask = np.linalg.norm(arr, axis=1) > 0.01
    if not valid_mask.any():
        return ["spk_?"] * n, [0.0] * n

    valid_idx = np.where(valid_mask)[0]
    valid = arr[valid_idx]
    valid = valid / (np.linalg.norm(valid, axis=1, keepdims=True) + 1e-9)

    if len(valid) == 1:
        labels = np.zeros(len(valid), dtype=int)
    else:
        try:
            from sklearn.cluster import AgglomerativeClustering
            cluster = AgglomerativeClustering(
                n_clusters=None,
                metric="cosine",
                linkage="average",
                distance_threshold=threshold,
            )
            labels = cluster.fit_predict(valid)
            # enforce MAX_SPEAKERS cap by merging smallest clusters
            uniq, counts = np.unique(labels, return_counts=True)
            if len(uniq) > max_speakers:
                # keep top-K, remap rest to nearest centroid among top-K
                top = uniq[np.argsort(-counts)[:max_speakers]]
                centroids = np.stack([
                    valid[labels == c].mean(axis=0) for c in top
                ])
                centroids /= np.linalg.norm(centroids, axis=1, keepdims=True) + 1e-9
                for c in uniq:
                    if c in top:
                        continue
                    mask = labels == c
                    sims = valid[mask] @ centroids.T
                    best = sims.argmax(axis=1)
                    labels[mask] = top[best]
        except Exception as exc:
            logger.warning(f"clustering fail: {exc!r}; falling back to single speaker")
            labels = np.zeros(len(valid), dtype=int)

    # Map cluster id to spk_N in encounter order
    spk_map: Dict[int, str] = {}
    spk_assign: List[str] = []
    confs: List[float] = []

    # compute centroids
    uniq = np.unique(labels)
    centroids: Dict[int, "np.ndarray"] = {}
    for c in uniq:
        members = valid[labels == c]
        cent = members.mean(axis=0)
        cent /= np.linalg.norm(cent) + 1e-9
        centroids[int(c)] = cent

    for i in range(n):
        if not valid_mask[i]:
            spk_assign.append("spk_?")
            confs.append(0.0)
            continue
        local_i = np.where(valid_idx == i)[0][0]
        c = int(labels[local_i])
        if c not in spk_map:
            spk_map[c] = f"spk_{len(spk_map)}"
        spk_assign.append(spk_map[c])
        sim = float(valid[local_i] @ centroids[c])
        # cosine sim in [-1,1] → conf in [0,1]
        conf = max(0.0, min(1.0, (sim + 1.0) / 2.0))
        confs.append(conf)

    logger.info(f"cluster: {len(spk_map)} speakers in {time.time() - t0:.1f}s")
    return spk_assign, confs


# -----------------------------------------------------------------------------
# Stage 6 — Save outputs (transcript.jsonl, opus, clip_index)
# -----------------------------------------------------------------------------
def write_outputs(
    session_id: str,
    input_path: Path,
    samples: "np.ndarray",
    segments: List[Tuple[float, float]],
    asr_out: List[Dict[str, Any]],
    embeds: List[List[float]],
    spk_assign: List[str],
    confs: List[float],
    output_dir: Path,
) -> List[Utterance]:
    import numpy as np
    import soundfile as sf
    import subprocess

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "clips").mkdir(exist_ok=True)

    # 6a — Build Utterance list, write transcript.jsonl
    utts: List[Utterance] = []
    transcript_path = output_dir / "transcript.jsonl"
    with transcript_path.open("w", encoding="utf-8") as fp:
        for i, ((s, e), asr, emb, spk, c) in enumerate(
            zip(segments, asr_out, embeds, spk_assign, confs)
        ):
            text = (asr.get("text") or "").strip()
            if not text:
                continue
            utt_id = f"{session_id}_{i:04d}"
            clip_rel = f"clips/{utt_id}.opus"
            utt = Utterance(
                utt_id=utt_id, idx=i,
                ts_start=float(s), ts_end=float(e),
                duration=float(e - s), text=text,
                avg_logprob=float(asr.get("avg_logprob", 0.0)),
                no_speech_prob=float(asr.get("no_speech_prob", 0.0)),
                language=asr.get("language") or "?",
                spk_local=spk, diariz_conf=float(c),
                voice_embed=emb, words=asr.get("words") or [],
                clip_uri=clip_rel,
            )
            utts.append(utt)
            fp.write(json.dumps(asdict(utt), ensure_ascii=False) + "\n")

    # 6b — Write VAD-merged clean Opus (L1 storage)
    # Concatenate all speech segments into one continuous audio for replay.
    if segments:
        clean_pcm_parts = []
        for s, e in segments:
            clean_pcm_parts.append(samples[int(s * TARGET_SR):int(e * TARGET_SR)])
        clean = np.concatenate(clean_pcm_parts) if clean_pcm_parts else np.zeros(0, dtype=np.float32)

        clean_wav = output_dir / "_tmp_clean.wav"
        sf.write(str(clean_wav), clean, TARGET_SR, subtype="PCM_16")
        clean_opus = output_dir / "clean.opus"
        ffmpeg = _ffmpeg_bin()
        try:
            subprocess.run([
                ffmpeg, "-y", "-loglevel", "error",
                "-i", str(clean_wav),
                "-c:a", "libopus", "-b:a", "24k", "-application", "voip",
                str(clean_opus),
            ], check=True)
            clean_wav.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning(f"opus encode fail (keeping wav): {exc!r}")

    # 6c — Per-utterance opus clips (for fast audio_anchor lookback)
    ffmpeg = _ffmpeg_bin()
    for utt in utts:
        s_idx, e_idx = int(utt.ts_start * TARGET_SR), int(utt.ts_end * TARGET_SR)
        clip_pcm = samples[s_idx:e_idx]
        tmp = output_dir / f"_tmp_{utt.utt_id}.wav"
        out = output_dir / utt.clip_uri
        sf.write(str(tmp), clip_pcm, TARGET_SR, subtype="PCM_16")
        try:
            subprocess.run([
                ffmpeg, "-y", "-loglevel", "error",
                "-i", str(tmp),
                "-c:a", "libopus", "-b:a", "24k", "-application", "voip",
                str(out),
            ], check=True)
        except Exception:
            # keep wav as fallback
            tmp.rename(out.with_suffix(".wav"))
        finally:
            tmp.unlink(missing_ok=True)

    # 6d — clip_index.json (utt_id → clip_uri + parent_audio)
    clip_index = {
        "session_id": session_id,
        "parent_audio": str(input_path),
        "sample_rate": TARGET_SR,
        "clips": {
            u.utt_id: {
                "clip_uri": u.clip_uri,
                "ts_start": u.ts_start,
                "ts_end": u.ts_end,
                "spk_local": u.spk_local,
            }
            for u in utts
        },
    }
    (output_dir / "clip_index.json").write_text(
        json.dumps(clip_index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return utts


# -----------------------------------------------------------------------------
# Top-level orchestrator
# -----------------------------------------------------------------------------
def process_audio_file(
    input_path: Path,
    output_dir: Path,
    session_id: Optional[str] = None,
) -> PipelineResult:
    """Full Stage 1-5 + write outputs. Returns PipelineResult."""
    if session_id is None:
        import hashlib
        digest = hashlib.sha256(input_path.read_bytes()).hexdigest()[:16]
        session_id = f"audio_{digest}"
    output_dir = output_dir / session_id
    output_dir.mkdir(parents=True, exist_ok=True)

    timing: Dict[str, float] = {}

    # 1. Decode
    t = time.time(); samples, dur = decode_to_pcm16k(input_path)
    timing["decode_s"] = time.time() - t

    # 2. VAD
    t = time.time(); segments = run_vad(samples)
    timing["vad_s"] = time.time() - t
    speech_dur = sum(e - s for s, e in segments)

    if not segments:
        logger.warning(f"VAD found no speech in {input_path}")
        result = PipelineResult(
            session_id=session_id, audio_input=str(input_path),
            duration_total=dur, duration_speech=0.0, speech_ratio=0.0,
            n_utterances=0, n_speakers=0,
            language_distribution={}, timing=timing,
            output_dir=str(output_dir),
        )
        (output_dir / "pipeline_stats.json").write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2),
            encoding="utf-8")
        return result

    # 3. ASR
    t = time.time(); asr_out = run_asr(samples, segments)
    timing["asr_s"] = time.time() - t

    # 4. Embeddings
    t = time.time(); embeds = run_embeddings(samples, segments)
    timing["ecapa_s"] = time.time() - t

    # 5. Cluster
    t = time.time(); spk_assign, confs = cluster_speakers(embeds)
    timing["cluster_s"] = time.time() - t

    # 6. Write outputs
    t = time.time()
    utts = write_outputs(session_id, input_path, samples, segments,
                         asr_out, embeds, spk_assign, confs, output_dir)
    timing["write_s"] = time.time() - t

    # Language distribution
    lang_dist: Dict[str, int] = {}
    for u in utts:
        lang_dist[u.language] = lang_dist.get(u.language, 0) + 1

    result = PipelineResult(
        session_id=session_id, audio_input=str(input_path),
        duration_total=dur, duration_speech=speech_dur,
        speech_ratio=speech_dur / dur if dur > 0 else 0.0,
        n_utterances=len(utts),
        n_speakers=len(set(u.spk_local for u in utts if u.spk_local != "spk_?")),
        language_distribution=lang_dist, timing=timing,
        output_dir=str(output_dir),
    )
    (output_dir / "pipeline_stats.json").write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2),
        encoding="utf-8")
    logger.info(f"DONE {session_id}: {result.n_utterances} utterances, "
                f"{result.n_speakers} speakers, "
                f"speech_ratio={result.speech_ratio:.1%}, "
                f"total wall={sum(timing.values()):.1f}s")
    return result


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Audio ASR + diarization pipeline")
    parser.add_argument("--input", required=True, help="Path to audio file")
    parser.add_argument("--output-dir", default="~/MEMEXA_audio/transcripts",
                        help="Base output dir; per-session subdir created")
    parser.add_argument("--session-id", default=None,
                        help="Override auto-derived session_id")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if not input_path.exists():
        print(f"input not found: {input_path}", file=sys.stderr)
        sys.exit(2)

    result = process_audio_file(input_path, output_dir, args.session_id)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
