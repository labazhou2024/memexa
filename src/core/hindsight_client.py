"""Thin httpx wrapper over Hindsight HindsightServer (fastapi+uvicorn).

Architecture:
- Hindsight daemon runs in py3.12 conda env (idle_timeout=0, long-running).
- Main memex (py3.9) talks to daemon via HTTP at MEMEX_HINDSIGHT_URL.
- This wrapper exposes 3-API contract: retain / recall / reflect.

OpenAPI endpoint paths (from hindsight-client 0.5.4 source @ 2026-04-25):
- POST /v1/default/banks/{bank_id}/memories          (retain)
- POST /v1/default/banks/{bank_id}/memories/recall   (recall)
- POST /v1/default/banks/{bank_id}/reflect           (reflect)

NOTE: hindsight-client 0.5.4 has a parser bug on reflect (ReflectBasedOn
expects dict but service returns list). We bypass the SDK and use httpx
directly for reflect to avoid the parser failure.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional, Union

try:
    import httpx
except ImportError:  # pragma: no cover - py3.9 base env may not have httpx
    httpx = None  # type: ignore

# Daemon URL. Single-host install: leave as default (loopback). Multi-host
# install (e.g. memory backend lives on a Mac, query CLI on Win): set
# ``MEMEX_HINDSIGHT_URL`` to the primary endpoint and (optionally)
# ``MEMEX_HINDSIGHT_FALLBACK_URL`` to a local mirror for HA. Calls fall
# through to the fallback automatically on timeout / connection refused.
_SERVER_URL = os.environ.get("MEMEX_HINDSIGHT_URL", "http://127.0.0.1:8888")
_FALLBACK_URL = os.environ.get("MEMEX_HINDSIGHT_FALLBACK_URL", "").strip()
# 2026-05-06 L0-v5: default bank switched to memory_full_v5 (schema:v2 Cards,
# chunks-only mode). memory_full_v3 frozen as archive (see data/l0_v5/v3_archived_marker.json).
# Set MEMEX_HINDSIGHT_BANK=memory_full_v3 to query legacy v3 bank explicitly.
_DEFAULT_BANK = os.environ.get("MEMEX_HINDSIGHT_BANK", "memory_full_v5")
_LEGACY_BANK = "memory_full"  # archived schema:v0 (queried only when explicit)
# 2026-04-30 daemon repair: bumped default 60s → 180s. Win CPU recall p50 ≈
# 60s warm + N×backfill contention; 60s often timed out under load. MEMEX_HINDSIGHT_TIMEOUT
# env still overrides. Mac Studio M4 Max 36GB (Metal MPS) is faster than Win CPU, so
# this default is conservative for Win-only deployment.
_DEFAULT_TIMEOUT = float(os.environ.get("MEMEX_HINDSIGHT_TIMEOUT", "180.0"))

# TU-U4-2 (2026-04-26): deterministic encoding probe.
# Per logic-iter1-1 fix: accept Union[bytes, str] with isinstance guard.
_TRACE_LOG_PATH = os.environ.get("MEMEX_HINDSIGHT_TRACE_LOG", "")


def _emit_trace(event: str, payload: dict[str, Any]) -> None:
    """Append trace event to JSONL when MEMEX_HINDSIGHT_TRACE_LOG is set.

    No-op when env unset (production daemon does not need to log every retain).
    """
    if not _TRACE_LOG_PATH:
        return
    try:
        rec = {"event": event, "ts": time.time(), **payload}
        with open(_TRACE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _decode_with_fallback(b: Union[bytes, str]) -> tuple[str, str]:
    """Deterministic encoding decoder with fallback ordering.

    Order: BOM check (utf-8-sig) -> utf-8 strict -> cp936 strict -> utf-8 errors=replace.

    Per autopilot v2.0 plan_v1 TU-U4-2 (logic-iter1-1 fix):
    isinstance(b, str) returns ('passthrough') to reject double-decode.

    Returns (decoded_text, encoding_used).
    Always emits ingest_encoding_chosen trace.
    """
    if isinstance(b, str):
        _emit_trace("ingest_encoding_chosen",
                    {"encoding": "passthrough", "byte_len": len(b),
                     "had_bom": False, "fallback_step": 0})
        return (b, "passthrough")

    # bytes path
    if not isinstance(b, (bytes, bytearray)):
        raise TypeError(f"_decode_with_fallback expects bytes or str, got {type(b).__name__}")

    raw = bytes(b)
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    byte_len = len(raw)

    # Step 1: BOM
    if had_bom:
        try:
            text = raw.decode("utf-8-sig")
            _emit_trace("ingest_encoding_chosen",
                        {"encoding": "utf-8-sig", "byte_len": byte_len,
                         "had_bom": True, "fallback_step": 1})
            return (text, "utf-8-sig")
        except UnicodeDecodeError:
            pass

    # Step 2: utf-8 strict
    try:
        text = raw.decode("utf-8")
        _emit_trace("ingest_encoding_chosen",
                    {"encoding": "utf-8", "byte_len": byte_len,
                     "had_bom": False, "fallback_step": 2})
        return (text, "utf-8")
    except UnicodeDecodeError:
        pass

    # Step 3: cp936 fallback (only on UnicodeDecodeError)
    try:
        text = raw.decode("cp936")
        _emit_trace("ingest_encoding_chosen",
                    {"encoding": "cp936", "byte_len": byte_len,
                     "had_bom": False, "fallback_step": 3})
        return (text, "cp936")
    except UnicodeDecodeError:
        pass

    # Step 4: last-resort replace
    text = raw.decode("utf-8", errors="replace")
    _emit_trace("ingest_encoding_chosen",
                {"encoding": "utf-8-replace", "byte_len": byte_len,
                 "had_bom": False, "fallback_step": 4})
    return (text, "utf-8-replace")


class HindsightHttpClient:
    """HTTP client to Hindsight daemon.

    Lazy-imports httpx so that module-level import in py3.9 (without httpx)
    does not break.
    """

    def __init__(
        self,
        base_url: str = _SERVER_URL,
        timeout: float = _DEFAULT_TIMEOUT,
        fallback_url: Optional[str] = None,
    ) -> None:
        if httpx is None:
            raise RuntimeError(
                "httpx not installed in this env. "
                "Install with: pip install httpx>=0.28"
            )
        self.base_url = base_url
        self.fallback_url = (
            fallback_url if fallback_url is not None else _FALLBACK_URL or None
        )
        self._timeout = timeout
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        self._http_fallback: Optional["httpx.Client"] = None
        if self.fallback_url:
            self._http_fallback = httpx.Client(base_url=self.fallback_url, timeout=timeout)

    def _request_with_failover(self, method: str, url: str, **kwargs) -> "httpx.Response":
        """Issue an HTTP call, retrying once on the fallback URL if set.

        Failover triggers on httpx connect / timeout exceptions. A 5xx
        response is NOT a failover trigger — those propagate so the caller
        sees the daemon-side error rather than masking it.
        """
        try:
            return self._http.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
            if self._http_fallback is None:
                raise
            _emit_trace("hindsight_failover", {
                "primary": self.base_url, "fallback": self.fallback_url,
                "exc": type(exc).__name__,
            })
            return self._http_fallback.request(method, url, **kwargs)

    def retain(
        self,
        content: Union[str, bytes],
        bank_id: str = _DEFAULT_BANK,
        context: Optional[str] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        async_: bool = True,
        document_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Store memory; LLM extracts facts in background.

        TU-U4-2: accepts bytes input; runs through _decode_with_fallback first.
        str input passes through unchanged (rejection of double-decode).

        2026-04-30 Hindsight 0.5.4 schema fix (memory_system_full_repair):
        wraps content in items=[{...}] array per RetainRequest schema; defaults
        to async=true so retain returns ~34ms (vs sync LLM extraction = 60s+
        on CPU). Caller can pass async_=False for sync write.

        Returns operation dict with operation_id for async tracking.
        """
        if isinstance(content, (bytes, bytearray)):
            content, _enc = _decode_with_fallback(bytes(content))
        item: dict[str, Any] = {"content": content}
        if context is not None:
            item["context"] = context
        if tags:
            item["tags"] = tags
        if metadata:
            item["metadata"] = metadata
        if document_id is not None:
            item["document_id"] = document_id
        body: dict[str, Any] = {"items": [item], "async": bool(async_)}
        # 2026-05-08: PG SQL_ASCII rejects \uXXXX escapes; send raw UTF-8
        # bytes (ensure_ascii=False) per post_pulled_cards_to_v5.py pattern.
        # Note: this alone does NOT fix non-ASCII metadata — v5 schema
        # requires b64-encoding CJK in metadata (see v5_baseline.json).
        # Caller must use MemoryCard V2 envelope or b64 transform metadata.
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        r = self._request_with_failover(
            "POST",
            f"/v1/default/banks/{bank_id}/memories",
            content=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        r.raise_for_status()
        result = r.json()
        # U12 cost_meter wiring (hot-path safe; swallow errors)
        try:
            from src.core.cost_meter import log as _cost_log
            _meta = result.get("usage") or {}
            _cost_log(
                "retain",
                model=result.get("model") or "deepseek-chat",
                prompt_tokens=int(_meta.get("prompt_tokens", 0)),
                completion_tokens=int(_meta.get("completion_tokens", 0)),
                bank_id=bank_id,
            )
        except Exception:
            pass
        return result

    def recall(
        self,
        query: str,
        bank_id: str = _DEFAULT_BANK,
        budget: str = "high",
        max_tokens: int = 4096,
        tags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Retrieve relevant facts via 4-way parallel retrieval + rerank."""
        body: dict[str, Any] = {"query": query, "budget": budget, "max_tokens": max_tokens}
        if tags:
            body["tags"] = tags
        r = self._request_with_failover(
            "POST", f"/v1/default/banks/{bank_id}/memories/recall", json=body
        )
        r.raise_for_status()
        result = r.json()
        # U12 cost_meter wiring (hot-path safe; swallow errors)
        try:
            from src.core.cost_meter import log as _cost_log
            _meta = result.get("usage") or {}
            _cost_log(
                "recall",
                model=result.get("model") or "deepseek-chat",
                prompt_tokens=int(_meta.get("prompt_tokens", 0)),
                completion_tokens=int(_meta.get("completion_tokens", 0)),
                budget=budget,
            )
        except Exception:
            pass
        return result

    def reflect(
        self,
        query: str,
        bank_id: str = _DEFAULT_BANK,
        budget: str = "low",
        max_tokens: int = 4096,
        include_facts: bool = False,
    ) -> dict[str, Any]:
        """LLM synthesizes natural-language answer from recalled facts.

        Bypasses hindsight-client SDK due to ReflectBasedOn parser bug.
        """
        body = {
            "query": query,
            "budget": budget,
            "max_tokens": max_tokens,
            "include_facts": include_facts,
        }
        r = self._request_with_failover(
            "POST", f"/v1/default/banks/{bank_id}/reflect", json=body
        )
        r.raise_for_status()
        result = r.json()
        # U12 cost_meter wiring (hot-path safe; swallow errors)
        try:
            from src.core.cost_meter import log as _cost_log
            _meta = result.get("usage") or {}
            _cost_log(
                "reflect",
                model=result.get("model") or "deepseek-chat",
                prompt_tokens=int(_meta.get("prompt_tokens", 0)),
                completion_tokens=int(_meta.get("completion_tokens", 0)),
                budget=budget,
            )
        except Exception:
            pass
        return result

    def health(self) -> bool:
        """Daemon liveness probe (primary URL only — does not failover)."""
        try:
            r = self._http.get("/health")
            return r.status_code == 200
        except Exception:
            return False

    def health_any(self) -> dict[str, bool]:
        """Probe primary + fallback. Returns ``{"primary": bool, "fallback": bool}``."""
        out: dict[str, bool] = {"primary": False, "fallback": False}
        try:
            out["primary"] = self._http.get("/health").status_code == 200
        except Exception:
            pass
        if self._http_fallback is not None:
            try:
                out["fallback"] = self._http_fallback.get("/health").status_code == 200
            except Exception:
                pass
        return out

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
        if self._http_fallback is not None:
            self._http_fallback.close()

    def __enter__(self) -> "HindsightHttpClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


# Module-level convenience functions
_client: Optional[HindsightHttpClient] = None


def get_client() -> HindsightHttpClient:
    global _client
    if _client is None:
        _client = HindsightHttpClient()
    return _client
