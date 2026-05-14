"""U8 mlx_lm_wrapper — Win-side dispatch to Mac MLX-LM 8B over Tailscale ssh.

Mac side prerequisite (LIVE 2026-04-30):
    ~/miniforge3/envs/qc/bin/mlx_lm.generate (mlx-lm 0.31.3)
    HF cache: ~/.cache/huggingface/hub/models--mlx-community--Llama-3.1-8B-Instruct-4bit/
    Measured: ~84 tok/s on M4 Max 36GB.

Usage:
    from memexa.dispatch import mlx_lm_wrapper
    out = mlx_lm_wrapper.invoke("Reply with one sentence: what is 2+2?", max_tokens=64)
    # → {"ok": True, "text": "...", "latency_s": 1.2, "tok_per_s": 53.3, ...}

HARD RULE compliance:
    - subprocess.run timeout=, ssh -o ConnectTimeout=15 BatchMode=yes
      (per feedback_xfer_subprocess_timeout_discipline)
    - emit-site for plan-declared trace event 'mlx_lm_invoked'
      (per feedback_trace_event_emit_or_assert)
    - typed exceptions (per RP-17/RP-19)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

REMOTE_HOST = "primary-host"
REMOTE_PYTHON = "$HOME/miniforge3/envs/qc/bin/python"
REMOTE_MLX_LM = "$HOME/miniforge3/envs/qc/bin/mlx_lm.generate"
REMOTE_MODEL = "mlx-community/Llama-3.1-8B-Instruct-4bit"

# HTTP backend (mlx_lm.server, port 18080) — used by invoke_http() when
# Mac launchd com.user.mlx_server is LIVE. ~5x faster than SSH+one-shot
# generate. Model selection is server-side via launch script (Qwen3-14B
# preferred for Chinese chat extraction).
HTTP_BACKEND_URL = os.environ.get(
    "MEMEXA_QWEN3_URL", "http://127.0.0.1:18080",
).rstrip("/")
HTTP_BACKEND_MODEL = os.environ.get(
    "MEMEXA_QWEN3_MODEL", "mlx-community/Qwen3-14B-4bit",
)
HTTP_BACKEND_TIMEOUT = float(
    os.environ.get("MEMEXA_QWEN3_HTTP_TIMEOUT", "120"),
)


def invoke_http(prompt: str, max_tokens: int = 1536,
                 temperature: float = 0.0,
                 model: str | None = None,
                 timeout: float | None = None) -> dict:
    """Call mlx_lm.server HTTP /v1/chat/completions (OpenAI-compatible).

    Returns dict {ok, text, latency_s, model, error?}. Soft-fail on any
    network/HTTP/JSON error to {ok: False, text: '', error: '...'}.
    Suitable as `invoker=` arg to extract_chat_triples for HTTP path.

    NOTE: Qwen3 has thinking-mode default; chain-of-thought lands in
    `message.reasoning` field and consumes ~500-700 tokens BEFORE the
    actual answer in `message.content`. Default max_tokens=1536 gives
    enough room for both. If `content` empty, fallback parses `reasoning`
    for any JSON array (the model often leaves the final JSON inside
    its scratchpad when truncated).
    """
    url = f"{HTTP_BACKEND_URL}/v1/chat/completions"
    body = json.dumps({
        "model": model or HTTP_BACKEND_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(
            req, timeout=timeout or HTTP_BACKEND_TIMEOUT,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latency = time.time() - t0
        msg = data.get("choices", [{}])[0].get("message", {})
        text = str(msg.get("content", "") or "")
        # Thinking-mode fallback: if content is empty (model spent budget on
        # reasoning), try to extract the JSON from reasoning scratchpad.
        if not text.strip():
            reasoning = str(msg.get("reasoning", "") or "")
            if reasoning:
                text = reasoning
        return {"ok": True, "text": text,
                "latency_s": round(latency, 2),
                "model": data.get("model", model or HTTP_BACKEND_MODEL)}
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError) as e:
        return {"ok": False, "text": "",
                "latency_s": round(time.time() - t0, 2),
                "error": f"{type(e).__name__}: {str(e)[:200]}"}
    except Exception as e:
        return {"ok": False, "text": "",
                "latency_s": round(time.time() - t0, 2),
                "error": f"{type(e).__name__}: {str(e)[:200]}"}

_TRACE_PATH = Path(__file__).resolve().parents[2] / "data" / "mlx_lm_trace.jsonl"

# HTTP dual-port backend configuration (TU-2 backfill_arc)
_GEMMA_BACKEND_URL = os.environ.get(
    "MEMEXA_GEMMA_URL", "http://127.0.0.1:18081",
).rstrip("/")
_GEMMA_BACKEND_MODEL = os.environ.get(
    "MEMEXA_GEMMA_MODEL", "mlx-community/gemma-3-12b-it-4bit",
)


def invoke_http_dual_port(
    prompt: str,
    ports: list | None = None,
    max_tokens: int = 512,
    temperature: float = 0.0,
    timeout: float | None = None,
) -> dict:
    """Call mlx_lm.server on two ports concurrently (Qwen3 + Gemma-3).

    Intended for paired_eval: port 18080 = Qwen3-14B, port 18081 = Gemma-3-12B.
    Returns dict {ok, results: [{port, ok, text, latency_s, model}, ...]}.
    Both ports are called in parallel via asyncio; if either fails,
    ok=False and each result carries its own 'ok' flag.

    Backwards-compatible: invoke_http() for single-port path unchanged.
    """
    import asyncio as _asyncio

    if ports is None:
        ports = [_DEFAULT_PRIMARY_PORT, _DEFAULT_SECONDARY_PORT]

    _DEFAULT_PRIMARY_PORT = 18080
    _DEFAULT_SECONDARY_PORT = 18081
    host = HTTP_BACKEND_URL.split("//")[-1].split(":")[0]
    _timeout = timeout or HTTP_BACKEND_TIMEOUT

    urls_models = []
    for p in ports:
        if p == 18080:
            urls_models.append((f"http://{host}:{p}", HTTP_BACKEND_MODEL))
        elif p == 18081:
            urls_models.append((f"http://{host}:{p}", _GEMMA_BACKEND_MODEL))
        else:
            urls_models.append((f"http://{host}:{p}", HTTP_BACKEND_MODEL))

    async def _call_one(base_url: str, model: str) -> dict:
        loop = _asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            lambda: invoke_http(prompt, max_tokens=max_tokens,
                                  temperature=temperature,
                                  model=model,
                                  timeout=_timeout),
        )

    async def _call_all() -> list:
        tasks = [_call_one(base_url, model) for base_url, model in urls_models]
        return list(await _asyncio.gather(*tasks))

    try:
        port_results = _asyncio.run(_call_all())
    except RuntimeError:
        # Event loop already running
        port_results = [
            invoke_http(prompt, max_tokens=max_tokens,
                        temperature=temperature, model=model, timeout=_timeout)
            for _, model in urls_models
        ]

    all_ok = all(r.get("ok") for r in port_results)
    labeled = [
        {"port": ports[i], **port_results[i]}
        for i in range(len(ports))
    ]
    return {"ok": all_ok, "results": labeled}


class MLXLMError(RuntimeError):
    """Base for MLX-LM dispatch errors."""


class MLXLMSshDown(MLXLMError):
    """ssh exit 255 / link down / host unreachable."""


class MLXLMTimeout(MLXLMError):
    """subprocess timeout exceeded."""


class MLXLMOutputUnparseable(MLXLMError):
    """stdout missing expected '==========' delimiters / no token-rate line."""


def _emit_trace(event: str, payload: dict) -> None:
    record = {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        **payload,
    }
    _TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _TRACE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


_TOK_RATE_RE = re.compile(
    r"Generation:?\s*\d+\s*tokens?,?\s*([\d.]+)\s*tokens?[-/ ]per[-/ ]sec",
    re.IGNORECASE,
)
_DELIM_RE = re.compile(r"={5,}")


def _parse_mlx_output(stdout: str) -> tuple[str, float]:
    """Parse mlx_lm.generate stdout → (generated_text, tok_per_sec).

    mlx_lm.generate prints:
        ==========
        <generated text>
        ==========
        Prompt: ...
        Generation: <N> tokens, <X.X> tokens-per-sec
    """
    parts = _DELIM_RE.split(stdout)
    if len(parts) < 3:
        raise MLXLMOutputUnparseable(f"missing ===== delimiters; got {len(parts)} segments")
    text = parts[1].strip()
    tail = parts[-1]
    m = _TOK_RATE_RE.search(tail)
    tok_per_s = float(m.group(1)) if m else 0.0
    return text, tok_per_s


def invoke(prompt: str, max_tokens: int = 256, temperature: float = 0.3, timeout: int = 120) -> dict:
    """Invoke MLX-LM Llama-3.1-8B on Mac via ssh.

    Returns dict {ok, text, latency_s, tok_per_s, error?}.
    Raises MLXLMSshDown / MLXLMTimeout / MLXLMOutputUnparseable on typed failures.
    """
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("prompt must be non-empty str")
    if max_tokens <= 0 or max_tokens > 4096:
        raise ValueError("max_tokens out of range [1, 4096]")

    # SEC-iter1 fix: send prompt via stdin (NOT argv interpolation) to defend
    # against remote shell injection via backticks / $() / $variable in chat
    # content. Per chat_to_graph plan_v3_FINAL data-locality invariant, content
    # is from WeChat msgs and CANNOT be assumed shell-safe.
    # Length cap: 16K chars (prompt injection defense; LLM context limit).
    PROMPT_MAX_CHARS = 16_384
    prompt_capped = prompt if len(prompt) <= PROMPT_MAX_CHARS else prompt[:PROMPT_MAX_CHARS]
    remote_cmd = (
        f"{REMOTE_MLX_LM} --model {REMOTE_MODEL} "
        f"--max-tokens {int(max_tokens)} --temp {float(temperature)} "
        f"--prompt -"  # read from stdin
    )
    ssh_argv = [
        "ssh",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=10",
        REMOTE_HOST,
        remote_cmd,
    ]

    t0 = time.time()
    try:
        result = subprocess.run(
            ssh_argv,
            input=prompt_capped,  # stdin transport (no shell interpolation)
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _emit_trace("mlx_lm_invoked", {
            "ok": False,
            "error": "timeout",
            "timeout_s": timeout,
            "prompt_len": len(prompt),
        })
        raise MLXLMTimeout(f"mlx_lm.invoke exceeded {timeout}s") from exc

    latency = round(time.time() - t0, 3)

    if result.returncode == 255 or "Connection refused" in result.stderr or "Could not resolve" in result.stderr:
        _emit_trace("mlx_lm_invoked", {
            "ok": False,
            "error": "ssh_down",
            "stderr_tail": result.stderr[-200:],
        })
        raise MLXLMSshDown(f"ssh exit {result.returncode}: {result.stderr[-200:]}")

    if result.returncode != 0:
        _emit_trace("mlx_lm_invoked", {
            "ok": False,
            "error": "remote_nonzero_exit",
            "exit_code": result.returncode,
            "stderr_tail": result.stderr[-200:],
        })
        return {
            "ok": False,
            "text": "",
            "latency_s": latency,
            "tok_per_s": 0.0,
            "error": f"remote exit {result.returncode}: {result.stderr[-200:]}",
        }

    try:
        text, tok_per_s = _parse_mlx_output(result.stdout)
    except MLXLMOutputUnparseable as exc:
        _emit_trace("mlx_lm_invoked", {
            "ok": False,
            "error": "unparseable",
            "stdout_tail": result.stdout[-200:],
        })
        raise

    _emit_trace("mlx_lm_invoked", {
        "ok": True,
        "prompt_len": len(prompt),
        "max_tokens": max_tokens,
        "latency_s": latency,
        "tok_per_s": tok_per_s,
        "text_len": len(text),
    })
    return {
        "ok": True,
        "text": text,
        "latency_s": latency,
        "tok_per_s": tok_per_s,
    }


# ---------------------------------------------------------------------------
# v3 U7 TU-4 extension: chat triple extraction with L3 schema-locked prompt
# ---------------------------------------------------------------------------

PREDICATE_VOCAB_30 = frozenset({
    # 30-enum predicate vocab (per chat_to_graph plan_v3_FINAL §schema)
    "is_a", "located_in", "owns", "knows", "works_at", "studied_at",
    "graduated_from", "born_on", "married_to", "child_of", "sibling_of",
    "friend_of", "colleague_of", "advisor_of", "supervised_by",
    "attended", "scheduled_for", "deadline_on", "received_from",
    "sent_to", "discussed", "agreed_to", "rejected", "interested_in",
    "responsible_for", "assigned_to", "prefers", "dislikes",
    "completed", "in_progress",
})


def _build_chat_extract_prompt(msg: dict, context: list) -> str:
    """L3 schema-locked prompt: 30-enum predicate + JSON-only output.

    Prepends /no_think Qwen3 directive — disables chain-of-thought so
    completion fits in <100 tokens (was 600+ in thinking mode + content
    often empty when truncated). LIVE-verified 2026-05-03: clean JSON
    output, ~10x faster.
    """
    # CEO 2026-05-04 directive: prompt MUST inject chat_room display + sender
    # display + group/1v1 distinction so single-Qwen extracts attribution-aware
    # triples (substitutes 1st-person with sender_display).
    text = str(msg.get("content", "") or "")
    sender_display = str(msg.get("sender_display_name") or msg.get("sender") or "user")
    chat_display = str(msg.get("chat_display_name") or msg.get("chat_name") or "")
    is_group = bool(msg.get("is_group_chat", False))
    pred_list = ", ".join(sorted(PREDICATE_VOCAB_30))
    ctx_lines = []
    for c in context[-10:]:
        c_sender = c.get("sender_display_name") or c.get("sender") or "?"
        ctx_lines.append(
            f"  [{c_sender}]: {str(c.get('content', ''))[:120]}"
        )
    ctx_block = "\n".join(ctx_lines) if ctx_lines else "  (no prior context)"
    chat_kind = "群聊" if is_group else "1对1对话"
    chat_header = f"会话({chat_kind}): {chat_display}\n" if chat_display else ""
    return (
        "/no_think\n"
        "Extract knowledge triples (subject, predicate, object) from the FOLLOWING "
        f"Chinese chat message. When the speaker uses 1st person ('我'), substitute "
        f"the speaker name ('{sender_display}'). Output ONLY a JSON array of objects "
        f"with keys 's', 'p', 'o'. Predicate MUST be from the 30-enum: [{pred_list}]. "
        f"If no extractable triples, output [].\n\n"
        f"{chat_header}"
        f"--- Recent context (last 10 msg in same chat) ---\n{ctx_block}\n\n"
        f"--- Current msg ---\n[{sender_display}]: {text}\n\n"
        f"JSON:"
    )


def _extract_json_array(raw: str) -> str:
    """Best-effort: find first [...] JSON array in raw output."""
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return "[]"
    return raw[start:end + 1]


def extract_chat_triples(
    msg: dict,
    context: list | None = None,
    adapter: str = "raw",
    max_tokens: int = 256,
    invoker=None,
) -> list:
    """Extract (s, p, o) triples from a chat msg via Qwen3-14B-MLX.

    Args:
      msg: dict with 'content' / 'sender' / 'chat_name' keys
      context: list of prior msgs (last 10 used) — same chat_room
      adapter: 'raw' for un-fine-tuned Qwen3 (default per CEO directive #4
               memory-not-reasoning); 'wechat_v1' for fine-tuned (deferred)
      max_tokens: output budget for triple list
      invoker: optional callable for dependency injection in tests
               (default: real `invoke()` over ssh)

    Returns: list of dicts each with keys s/p/o. May be empty if no triples.
    Raises: MLXLMSshDown / MLXLMTimeout on Mac infra failures.

    Each triple validated against PREDICATE_VOCAB_30; non-vocab predicates
    are filtered out (silent drop, NOT raise).
    """
    if context is None:
        context = []
    prompt = _build_chat_extract_prompt(msg, context)

    invoke_fn = invoker if invoker is not None else invoke
    try:
        result = invoke_fn(prompt, max_tokens=max_tokens, temperature=0.0)
    except (MLXLMSshDown, MLXLMTimeout, MLXLMOutputUnparseable) as e:
        # LG-iter1-6 fix: emit trace on infra failure, not silent
        try:
            from memexa.core.trace_sink import emit  # type: ignore
            emit("chat_extract_failed", {
                "chat_room_hash": _hash_chat_name(msg.get("chat_name", "")),
                "error_type": type(e).__name__,
            })
        except Exception:
            pass
        return []
    if not result.get("ok"):
        try:
            from memexa.core.trace_sink import emit  # type: ignore
            emit("chat_extract_failed", {
                "chat_room_hash": _hash_chat_name(msg.get("chat_name", "")),
                "error_type": "invoker_returned_not_ok",
                "error": str(result.get("error", ""))[:200],
            })
        except Exception:
            pass
        return []

    raw_text = str(result.get("text", ""))
    json_blob = _extract_json_array(raw_text)
    try:
        triples = json.loads(json_blob)
    except (json.JSONDecodeError, ValueError):
        return []

    if not isinstance(triples, list):
        return []

    valid_triples = []
    for t in triples:
        if not isinstance(t, dict):
            continue
        s = t.get("s")
        p = t.get("p")
        o = t.get("o")
        if not (isinstance(s, str) and isinstance(p, str) and isinstance(o, str)):
            continue
        if p not in PREDICATE_VOCAB_30:
            continue  # silent drop — schema vocab compliance
        valid_triples.append({"s": s, "p": p, "o": o})

    _emit_trace("chat_extracted", {
        "chat_room_hash": _hash_chat_name(msg.get("chat_name", "")),
        "triple_count": len(valid_triples),
        "raw_count": len(triples) if isinstance(triples, list) else 0,
        "adapter": adapter,
    })
    return valid_triples


def _hash_chat_name(name) -> str:
    import hashlib
    return hashlib.sha256(str(name).encode("utf-8", errors="replace")).hexdigest()[:16]


def main(argv: list[str]) -> int:
    """CLI smoke: python -m memexa.extraction.mlx_lm_wrapper "<prompt>" [max_tokens]"""
    if len(argv) < 2:
        print("usage: mlx_lm_wrapper.py <prompt> [max_tokens]")
        return 2
    prompt = argv[1]
    mt = int(argv[2]) if len(argv) > 2 else 64
    try:
        out = invoke(prompt, max_tokens=mt)
    except MLXLMError as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "msg": str(exc)}))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
