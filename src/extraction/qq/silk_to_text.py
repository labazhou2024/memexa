"""silk_to_text.py — voice → text inline transcription for L0 v5 QQ + WeChat.

Goal: take a path to a voice file (silk-v3 / amr / mp3 / wav / m4a) and
produce a short text transcription. Caller embeds the text into the batch
prompt as an utterance with prefix `[语音转写] `.

Design choices (per CEO directive 2026-05-06):
  - "实际分析 batch 的时候顺手处理, 无需单独开启一个进程"
  - inline within batch generation (Win-side); no daemon
  - idempotent disk cache: keyed by sha256(file_bytes) → cache hit returns instantly
  - fail-soft: any failure returns ("[语音 (STT 失败: <reason>)]", err_kind)
  - voice file may already have been transcoded by WeChat client; we
    detect and ffmpeg-convert to 16kHz mono wav before Whisper.

Decoder chain:
  1. .amr / .mp3 / .m4a / .wav / .ogg  → ffmpeg → 16k mono wav
  2. .silk / .slk (raw silk-v3, QQ + recent WeChat) → silk_decoder → pcm → ffmpeg → wav
     - silk_decoder may be missing on this machine; fallback to placeholder
  3. wav → faster_whisper (small, multilingual, CPU is fine for batch mode)

Cache layout: data/l0_v5_qq/work/stt_cache/<sha256-prefix>/<sha256>.json
  {audio_sha256, transcribed_text, model, lang_detected, duration_s,
   created_at, decoder_chain}

Public:
  transcribe_voice_file(path: Path) -> dict {ok, text, error, ...}
  embed_voice_marker(text: str) -> str  (prepends [语音转写] tag)

Tests: tests/l0_v5_qq/test_silk_to_text.py
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ────────────────────────── module config ──────────────────────────

_REPO = Path(__file__).resolve().parents[3]
_CACHE_DIR = _REPO / "data" / "l0_v5_qq" / "work" / "stt_cache"
_DEFAULT_MODEL = os.environ.get("MEMEX_STT_MODEL", "small")
_DEFAULT_LANG = os.environ.get("MEMEX_STT_LANG", "zh")
_FFMPEG = os.environ.get("MEMEX_FFMPEG", "ffmpeg")
_SILK_DECODER = os.environ.get("MEMEX_SILK_DECODER", "silk_v3_decoder")

VOICE_TAG = "[语音转写]"
VOICE_FAIL_TAG = "[语音"  # prefix used in fail-soft placeholders


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_path_for(sha: str) -> Path:
    return _CACHE_DIR / sha[:2] / f"{sha}.json"


def _read_cache(sha: str) -> Optional[dict]:
    cp = _cache_path_for(sha)
    if not cp.exists():
        return None
    try:
        return json.loads(cp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(sha: str, payload: dict) -> None:
    cp = _cache_path_for(sha)
    cp.parent.mkdir(parents=True, exist_ok=True)
    try:
        cp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    except OSError:
        pass


# ────────────────────────── decoders ──────────────────────────

def _have_tool(name: str) -> bool:
    return shutil.which(name) is not None


def _ffmpeg_to_wav(src: Path, dst: Path, timeout_s: float = 30.0) -> Tuple[bool, str]:
    """Convert any ffmpeg-readable audio → 16k mono wav. Returns (ok, error)."""
    if not _have_tool(_FFMPEG):
        return False, "ffmpeg_not_found"
    cmd = [
        _FFMPEG, "-y", "-i", str(src),
        "-ar", "16000", "-ac", "1", "-f", "wav", str(dst),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
        if r.returncode != 0:
            return False, f"ffmpeg_rc={r.returncode}"
        if not dst.exists() or dst.stat().st_size < 44:
            return False, "ffmpeg_empty_output"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "ffmpeg_timeout"
    except OSError as e:
        return False, f"ffmpeg_oserror={e!r}"


def _silk_to_pcm(src: Path, dst_pcm: Path, timeout_s: float = 30.0) -> Tuple[bool, str]:
    """Decode silk-v3 → raw 16k pcm via external silk_v3_decoder.

    silk_v3_decoder is NOT bundled; absence is expected on this machine.
    Returns (ok, err); ok=False with err='silk_decoder_not_found' is a
    legitimate fail-soft case (caller falls back to placeholder).
    """
    if not _have_tool(_SILK_DECODER):
        return False, "silk_decoder_not_found"
    cmd = [_SILK_DECODER, str(src), str(dst_pcm), "-Fs_API", "16000"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
        if r.returncode != 0:
            return False, f"silk_rc={r.returncode}"
        if not dst_pcm.exists() or dst_pcm.stat().st_size < 100:
            return False, "silk_empty_output"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "silk_timeout"
    except OSError as e:
        return False, f"silk_oserror={e!r}"


def _detect_format(path: Path) -> str:
    """Cheap header sniff. Returns one of: silk, amr, mp3, m4a, ogg, wav, unknown."""
    suffix = path.suffix.lower().lstrip(".")
    if suffix in ("silk", "slk"):
        return "silk"
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return "unknown"
    if head.startswith(b"\x02#!SILK_V3") or b"#!SILK_V3" in head[:16]:
        return "silk"
    if head.startswith(b"#!AMR"):
        return "amr"
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav"
    return suffix or "unknown"


# ────────────────────────── whisper ──────────────────────────

_WHISPER_CACHE = {}


def _get_whisper(model_name: str):
    if model_name in _WHISPER_CACHE:
        return _WHISPER_CACHE[model_name]
    try:
        from faster_whisper import WhisperModel
        device = os.environ.get("MEMEX_STT_DEVICE", "cpu")
        compute_type = os.environ.get("MEMEX_STT_COMPUTE", "int8")
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        _WHISPER_CACHE[model_name] = model
        return model
    except ImportError:
        return None
    except (RuntimeError, OSError) as e:
        logger.warning("whisper init failed: %r", e)
        return None


def _whisper_transcribe(wav_path: Path, model_name: str, lang: str) -> Tuple[str, dict]:
    """Run faster-whisper. Returns (text, meta_dict). Empty text on failure."""
    model = _get_whisper(model_name)
    if model is None:
        return "", {"error": "whisper_unavailable"}
    try:
        segments, info = model.transcribe(
            str(wav_path), language=lang, beam_size=1,
            vad_filter=True, condition_on_previous_text=False,
        )
        text = "".join(seg.text for seg in segments).strip()
        meta = {
            "lang_detected": info.language,
            "lang_prob": float(info.language_probability),
            "duration_s": float(info.duration),
        }
        return text, meta
    except (RuntimeError, OSError) as e:
        return "", {"error": f"whisper_failed={e!r}"}


# ────────────────────────── public API ──────────────────────────

def transcribe_voice_file(
    path: Path | str,
    model_name: Optional[str] = None,
    lang: Optional[str] = None,
    use_cache: bool = True,
    max_seconds: float = 600.0,
) -> dict:
    """Transcribe one voice file to text. Idempotent via sha256 cache.

    Returns dict:
        {ok: bool, text: str, error: Optional[str], audio_sha256: str,
         decoder_chain: list[str], model: str, ts: float}

    text is ALWAYS a non-empty string; on failure it is a `[语音 (STT 失败: ...)]`
    placeholder so caller can blindly embed it. Check `ok` for hard success.
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        return {
            "ok": False, "text": f"{VOICE_FAIL_TAG} (文件不存在)]",
            "error": "file_not_found", "audio_sha256": "",
            "decoder_chain": [], "model": "", "ts": time.time(),
        }

    model_name = model_name or _DEFAULT_MODEL
    lang = lang or _DEFAULT_LANG

    audio_sha = _sha256_file(p)
    if use_cache:
        cached = _read_cache(audio_sha)
        if cached and cached.get("ok"):
            cached["from_cache"] = True
            return cached

    fmt = _detect_format(p)
    decoder_chain = [f"detect={fmt}"]

    with tempfile.TemporaryDirectory(prefix="stt_") as td:
        td = Path(td)
        wav = td / "out.wav"

        if fmt == "silk":
            pcm = td / "out.pcm"
            ok, err = _silk_to_pcm(p, pcm)
            decoder_chain.append(f"silk={ok}")
            if not ok:
                payload = {
                    "ok": False,
                    "text": f"{VOICE_FAIL_TAG} (silk 解码不可用: {err})]",
                    "error": err, "audio_sha256": audio_sha,
                    "decoder_chain": decoder_chain, "model": model_name,
                    "ts": time.time(),
                }
                if use_cache:
                    _write_cache(audio_sha, payload)
                return payload
            ok2, err2 = _ffmpeg_to_wav(pcm, wav)
            decoder_chain.append(f"ffmpeg(pcm→wav)={ok2}")
            if not ok2:
                return {
                    "ok": False, "text": f"{VOICE_FAIL_TAG} (ffmpeg 失败: {err2})]",
                    "error": err2, "audio_sha256": audio_sha,
                    "decoder_chain": decoder_chain, "model": model_name,
                    "ts": time.time(),
                }
        else:
            ok, err = _ffmpeg_to_wav(p, wav)
            decoder_chain.append(f"ffmpeg={ok}")
            if not ok:
                payload = {
                    "ok": False, "text": f"{VOICE_FAIL_TAG} (ffmpeg 失败: {err})]",
                    "error": err, "audio_sha256": audio_sha,
                    "decoder_chain": decoder_chain, "model": model_name,
                    "ts": time.time(),
                }
                if use_cache:
                    _write_cache(audio_sha, payload)
                return payload

        text, meta = _whisper_transcribe(wav, model_name, lang)
        decoder_chain.append(f"whisper={'ok' if text else 'empty'}")

        if not text:
            payload = {
                "ok": False, "text": f"{VOICE_FAIL_TAG} (whisper 无输出)]",
                "error": meta.get("error", "whisper_empty"),
                "audio_sha256": audio_sha,
                "decoder_chain": decoder_chain, "model": model_name,
                "ts": time.time(),
            }
            if use_cache:
                _write_cache(audio_sha, payload)
            return payload

        if meta.get("duration_s", 0) > max_seconds:
            text = f"{text[:300]}…[过长截断]"

        payload = {
            "ok": True,
            "text": embed_voice_marker(text),
            "error": None,
            "audio_sha256": audio_sha,
            "decoder_chain": decoder_chain,
            "model": model_name,
            "ts": time.time(),
            **meta,
        }
        if use_cache:
            _write_cache(audio_sha, payload)
        return payload


def embed_voice_marker(text: str) -> str:
    """Prepend the voice tag for downstream LLM disambiguation."""
    if not text:
        return f"{VOICE_FAIL_TAG} (空)]"
    if text.startswith(VOICE_TAG):
        return text
    return f"{VOICE_TAG} {text}".strip()


def cli() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--model", default=None)
    ap.add_argument("--lang", default=None)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    out = transcribe_voice_file(
        Path(args.path), model_name=args.model, lang=args.lang,
        use_cache=not args.no_cache,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 2


if __name__ == "__main__":
    import sys
    sys.exit(cli())
