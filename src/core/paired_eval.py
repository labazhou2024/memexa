"""DEPRECATED — paired_eval permanently archived 2026-05-07.

CEO directive (2026-05-07):
> "mac那一套永久归档，gemma 31B能力非常强，再过一遍qwen我测试过基本没用"

The Mac dual-model paired_eval stack is dead code. L0 v5 pipeline (your-org
Gemma 4 31B AWQ as sole extractor + Qwen3-14B as gatekeeper-only) is the
authoritative path. Cards are written with `attestation_tier="paired_v2"`
by default; cross-model verification is OFF.

Archived files: archive/2026_05_07_paired_eval_archived/

This shim exists so any legacy callers (src.extraction.batch_chat_extract,
batch_classifier, chat_extract_local, tools.backfill_full_pipeline,
tools.phase1_pipeline) import without crashing — but every call raises
PairedEvalDeprecatedError. Callers should be migrated to L0 v5 path or
removed.
"""
from __future__ import annotations

import warnings
from typing import Any, Dict, List, Optional


class PairedEvalError(RuntimeError):
    """Backward-compat exception name."""


class CrossModelUnavailableError(PairedEvalError):
    """Backward-compat: raised when Mac dual-LLM unreachable."""


class HostNotAllowedError(PairedEvalError):
    """Backward-compat: host validation rejection."""


class PairedEvalDeprecatedError(PairedEvalError):
    """Raised on any actual paired_eval invocation post-archival."""


_WARNED = False


def _warn_once() -> None:
    global _WARNED
    if _WARNED:
        return
    _WARNED = True
    warnings.warn(
        "paired_eval is permanently archived (CEO directive 2026-05-07). "
        "L0 v5 pipeline uses Gemma 4 31B sole-extractor; no Mac dual-LLM. "
        "Migrate caller to L0 v5 or remove. "
        "Archived sources: archive/2026_05_07_paired_eval_archived/",
        DeprecationWarning,
        stacklevel=3,
    )


def paired_extract(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    _warn_once()
    raise PairedEvalDeprecatedError(
        "paired_extract: archived 2026-05-07. Use L0 v5 pipeline instead."
    )


def paired_extract_with_route(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    _warn_once()
    raise PairedEvalDeprecatedError(
        "paired_extract_with_route: archived 2026-05-07."
    )


def calibrate(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    _warn_once()
    raise PairedEvalDeprecatedError("calibrate: archived 2026-05-07.")


def _get_validated_host() -> str:
    """Stub for legacy phase1_pipeline import (2026-05-08).

    Returns env MEMEX_MAC_HOST if set, else 'localhost'. No actual validation
    since Mac MLX path is archived; phase1_pipeline server backend uses your-org
    HTTP and never invokes this for real routing.
    """
    import os
    return os.environ.get("MEMEX_MAC_HOST", "localhost")


def _validate_host(host: str) -> str:
    return host


def sanitize_for_llm(text: str) -> str:
    """Trim + strip null bytes + cap length (defensive)."""
    if not isinstance(text, str):
        text = str(text)
    text = text.replace("\x00", "")
    if len(text) > 32000:
        text = text[:32000]
    return text


def _maybe_transform_for_arbiter(prompt: str, model: str) -> str:
    """Gemma 4 family arbiter: strip /no_think + inject emotional block.
    Other models pass through unchanged.

    Restored 2026-05-08 from archive (needed by phase1 Stage B).
    """
    if "gemma-4" not in (model or "").lower():
        return prompt
    if prompt.startswith("/no_think\n"):
        prompt = prompt[len("/no_think\n"):]
    return prompt  # full emotional injection skipped (only used in dual-LLM mode)


def _http_call_sync(url: str, prompt: str, model: str,
                    timeout: float = 180.0,
                    max_tokens: int = 512) -> Dict[str, Any]:
    """Synchronous HTTP call to vLLM /v1/chat/completions.

    Restored 2026-05-08 (phase1 Stage A/B workers). Simplified from socket-
    level archive version: uses httpx with explicit timeouts. Returns
    {ok, text, status_code, error?}.
    """
    import httpx as _httpx
    # Detect Gemma (Stage B extractor) vs Qwen3 (Stage A gate). vLLM
    # served-names are "memex-extractor" (Gemma) and "memex-primary" (Qwen3),
    # neither contains "gemma-4" — match on broader signals.
    m = (model or "").lower()
    is_gemma = ("gemma" in m) or ("extractor" in m)
    if is_gemma:
        user_content = prompt
    else:
        # Qwen3 /no_think directive — vLLM 2026-05 does NOT honor
        # chat_template_kwargs reliably (verified empty <think> block still
        # generated). In-prompt directive is Qwen3-native and works.
        user_content = (prompt or "").rstrip() + "\n\n/no_think"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": user_content}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": False,
    }
    if is_gemma:
        body["chat_template_kwargs"] = {"enable_thinking": True}
    else:
        # Belt + suspenders alongside /no_think directive
        body["chat_template_kwargs"] = {"enable_thinking": False}
    full_url = url.rstrip("/")
    if not full_url.endswith("/v1/chat/completions"):
        if "/v1/chat/completions" not in full_url:
            full_url = full_url + "/v1/chat/completions"
    try:
        with _httpx.Client(timeout=_httpx.Timeout(timeout, connect=10.0)) as c:
            r = c.post(full_url, json=body)
            if r.status_code != 200:
                return {"ok": False, "status_code": r.status_code,
                        "text": "", "error": (r.text or "")[:200]}
            d = r.json()
            text = ""
            try:
                text = d["choices"][0]["message"]["content"]
            except Exception:
                pass
            return {"ok": True, "text": text, "status_code": 200,
                    "usage": d.get("usage", {})}
    except _httpx.TimeoutException as e:
        return {"ok": False, "text": "", "status_code": 0,
                "error": f"timeout: {e}"[:200]}
    except Exception as e:
        return {"ok": False, "text": "", "status_code": 0,
                "error": f"{type(e).__name__}: {str(e)[:200]}"}


# Constants kept for type-import compatibility (some legacy modules import
# these even when they don't call functions). Empty / safe defaults.
ATTESTATION_TIERS_LEGACY = ("paired_v1", "paired_v2", "arbiter_27b",
                             "rag_assisted_v2", "legacy_unverified")


__all__ = [
    "PairedEvalError",
    "CrossModelUnavailableError",
    "HostNotAllowedError",
    "PairedEvalDeprecatedError",
    "paired_extract",
    "paired_extract_with_route",
    "calibrate",
    "ATTESTATION_TIERS_LEGACY",
]
