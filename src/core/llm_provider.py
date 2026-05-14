"""Unified LLM provider abstraction — swap Claude/OpenAI/GLM without code changes.

Usage:
    from src.core.llm_provider import call_llm
    result = call_llm(
        user="extract facts from this",
        system="respond with JSON array only",
        tier="cheap_fast",   # or "smart", "premium"
        timeout=30,
    )

Provider selection order:
    1. `provider=` kwarg (explicit override)
    2. env `MEMEXA_LLM_PROVIDER` ∈ {claude, openai, glm}
    3. default: "claude"

Tier → model map (configured in memexa/data/llm_provider_config.json):
    cheap_fast : haiku-like (fast, cheap)
    smart      : sonnet-like (balanced)
    premium    : opus-like (highest quality)

Each backend implements: `call(prompt, system, model, timeout) -> str`.
Returns the raw text response. Callers do their own JSON parsing.

Switching procedure when Claude CLI down:
    1. Acquire API key for OpenAI or GLM (智谱清言)
    2. setx MEMEXA_LLM_PROVIDER openai  (Windows) OR export ... (POSIX)
    3. setx OPENAI_API_KEY sk-...
    4. Restart Claude Code sessions
    5. All call_llm() automatically routes through new provider
    (See feedback_llm_provider_switch_guide.md for full steps.)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Dict, Any

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "llm_provider_config.json"

# Fallback default config if file missing
_DEFAULT_CONFIG = {
    "default_provider": "claude",
    "providers": {
        "claude": {
            "tier_model_map": {
                "cheap_fast": "claude-haiku-4-5",
                "smart":      "claude-sonnet-4-6",
                "premium":    "claude-opus-4-7",
            },
            "requires_env": [],
            "cli": "claude",
        },
        "openai": {
            "tier_model_map": {
                "cheap_fast": "gpt-4o-mini",
                "smart":      "gpt-4o",
                "premium":    "gpt-4-turbo",
            },
            "requires_env": ["OPENAI_API_KEY"],
            "base_url": "https://api.openai.com/v1/chat/completions",
        },
        "glm": {
            "tier_model_map": {
                "cheap_fast": "glm-4-flash",
                "smart":      "glm-4-plus",
                "premium":    "glm-4-long",
            },
            "requires_env": ["GLM_API_KEY"],
            "base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        },
    },
}


class LLMError(RuntimeError):
    """Raised on unrecoverable LLM backend failure."""


class ProviderUnavailable(LLMError):
    """Raised when provider cannot be initialized (missing key/CLI/etc)."""


def _load_config() -> Dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("llm_provider: config parse failed, using default: %s", e)
    return _DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------

class Backend(Protocol):
    provider_name: str

    def call(self, user: str, system: str, model: str, timeout: int) -> str:
        ...


# ---------------------------------------------------------------------------
# Claude CLI backend (default — uses claude -p via subprocess)
# ---------------------------------------------------------------------------

class ClaudeCliBackend:
    provider_name = "claude"

    def call(self, user: str, system: str, model: str, timeout: int) -> str:
        try:
            from src.core.subprocess_launcher import claude_argv
        except ImportError:
            raise ProviderUnavailable("claude subprocess_launcher not available")

        cmd = claude_argv([
            "-p", "--model", model,
            "--output-format", "text",
            "--max-turns", "1",
        ])
        if system:
            # Insert --system-prompt AFTER -p flag
            idx = cmd.index("-p") + 1
            cmd = cmd[:idx] + ["--system-prompt", system] + cmd[idx:]

        # Scrub CLAUDE_*/MEMEXA_* env to avoid SessionStart hook hijack
        # (see feedback_claude_subprocess_env_scrub.md)
        clean_env = {k: v for k, v in os.environ.items()
                     if not k.startswith("CLAUDE_") and not k.startswith("MEMEXA_")}
        clean_env["PATH"] = os.environ.get("PATH", "")

        try:
            r = subprocess.run(
                cmd, input=user, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout,
                cwd=os.path.expanduser("~"),
                env=clean_env,
            )
        except FileNotFoundError:
            raise ProviderUnavailable("claude CLI not found in PATH")
        except subprocess.TimeoutExpired:
            raise LLMError(f"claude CLI timeout after {timeout}s")

        if r.returncode != 0:
            raise LLMError(f"claude CLI rc={r.returncode} stderr={r.stderr[:200]!r}")
        return r.stdout.strip()


# ---------------------------------------------------------------------------
# OpenAI backend (HTTP POST to chat/completions)
# ---------------------------------------------------------------------------

class OpenAIBackend:
    provider_name = "openai"

    def __init__(self, base_url: str = "https://api.openai.com/v1/chat/completions"):
        self.base_url = base_url

    def call(self, user: str, system: str, model: str, timeout: int) -> str:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise ProviderUnavailable("OPENAI_API_KEY not set")
        try:
            import urllib.request
            import urllib.error
        except ImportError:
            raise ProviderUnavailable("urllib not available")

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        body = json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": 2000,
            "temperature": 0.3,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise LLMError(f"OpenAI HTTP {e.code}: {e.read()[:200]!r}")
        except (urllib.error.URLError, OSError) as e:
            raise LLMError(f"OpenAI network error: {e}")
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"OpenAI response schema mismatch: {e}; body={str(data)[:200]!r}")


# ---------------------------------------------------------------------------
# GLM backend (智谱清言 — OpenAI-compatible /chat/completions endpoint)
# ---------------------------------------------------------------------------

class GLMBackend:
    provider_name = "glm"

    def __init__(self, base_url: str = "https://open.bigmodel.cn/api/paas/v4/chat/completions"):
        self.base_url = base_url

    def call(self, user: str, system: str, model: str, timeout: int) -> str:
        api_key = os.environ.get("GLM_API_KEY", "")
        if not api_key:
            raise ProviderUnavailable("GLM_API_KEY not set")
        try:
            import urllib.request
            import urllib.error
        except ImportError:
            raise ProviderUnavailable("urllib not available")

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        body = json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": 2000,
            "temperature": 0.3,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise LLMError(f"GLM HTTP {e.code}: {e.read()[:200]!r}")
        except (urllib.error.URLError, OSError) as e:
            raise LLMError(f"GLM network error: {e}")
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"GLM response schema mismatch: {e}; body={str(data)[:200]!r}")


# ---------------------------------------------------------------------------
# DeepSeek backend (OpenAI-compatible /chat/completions endpoint)
# Per CEO 2026-04-28: external/judge model uses DeepSeek V4 Pro;
# DEEPSEEK_API_KEY env channel separate from memory system key.
# ---------------------------------------------------------------------------

class DeepSeekBackend:
    """DeepSeek OpenAI-compatible backend.

    Models supported:
        deepseek-v4-pro (default judge_model for U16 cross_model_gate)
        deepseek-chat
        deepseek-reasoner
    """
    provider_name = "deepseek"

    # security-iter1-5 LOW fix: prefix allowlist for base_url to prevent
    # config-redirect attacks (Authorization header + claim payload exfil).
    _ALLOWED_BASE_URL_PREFIXES = (
        "https://api.deepseek.com",
    )

    def __init__(self, base_url: str = "https://api.deepseek.com/chat/completions"):
        if not any(base_url.startswith(p) for p in self._ALLOWED_BASE_URL_PREFIXES):
            raise ProviderUnavailable(
                f"DeepSeek base_url={base_url!r} does not match allowlist "
                f"{self._ALLOWED_BASE_URL_PREFIXES}; potential redirect attack"
            )
        self.base_url = base_url

    def call(self, user: str, system: str, model: str, timeout: int) -> str:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise ProviderUnavailable("DEEPSEEK_API_KEY not set")
        try:
            import urllib.request
            import urllib.error
        except ImportError:
            raise ProviderUnavailable("urllib not available")

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        body = json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": 2000,
            "temperature": 0.3,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.base_url,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise LLMError(f"DeepSeek HTTP {e.code}: {e.read()[:200]!r}")
        except (urllib.error.URLError, OSError) as e:
            raise LLMError(f"DeepSeek network error: {e}")
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"DeepSeek response schema mismatch: {e}; body={str(data)[:200]!r}")


# ---------------------------------------------------------------------------
# Registry + public API
# ---------------------------------------------------------------------------

_BACKENDS: Dict[str, Backend] = {}


def _get_backend(provider: str) -> Backend:
    if provider in _BACKENDS:
        return _BACKENDS[provider]
    cfg = _load_config()
    prov_cfg = cfg.get("providers", {}).get(provider, {})
    if provider == "claude":
        backend = ClaudeCliBackend()
    elif provider == "openai":
        backend = OpenAIBackend(base_url=prov_cfg.get("base_url",
            "https://api.openai.com/v1/chat/completions"))
    elif provider == "glm":
        backend = GLMBackend(base_url=prov_cfg.get("base_url",
            "https://open.bigmodel.cn/api/paas/v4/chat/completions"))
    elif provider == "deepseek":
        backend = DeepSeekBackend(base_url=prov_cfg.get("base_url",
            "https://api.deepseek.com/chat/completions"))
    else:
        raise ProviderUnavailable(f"unknown provider: {provider}")
    _BACKENDS[provider] = backend
    return backend


def resolve_provider(override: Optional[str] = None) -> str:
    """Priority: explicit arg > MEMEXA_LLM_PROVIDER env > config default > 'claude'."""
    if override:
        return override
    env = os.environ.get("MEMEXA_LLM_PROVIDER", "").strip().lower()
    if env in {"claude", "openai", "glm", "deepseek"}:
        return env
    cfg = _load_config()
    return cfg.get("default_provider", "claude")


def resolve_model(provider: str, tier: str) -> str:
    cfg = _load_config()
    tmap = cfg.get("providers", {}).get(provider, {}).get("tier_model_map", {})
    if tier in tmap:
        return tmap[tier]
    # Fallback to default
    default_tmap = _DEFAULT_CONFIG["providers"][provider]["tier_model_map"]
    return default_tmap.get(tier, default_tmap["cheap_fast"])


def call_llm(user: str, system: str = "", tier: str = "cheap_fast",
             timeout: int = 45, provider: Optional[str] = None) -> str:
    """Call an LLM via the selected provider. Returns text response.

    Raises:
        ProviderUnavailable: backend init failed (missing key/CLI)
        LLMError: call failed at runtime (HTTP error, timeout, parse error)
    """
    prov = resolve_provider(provider)
    model = resolve_model(prov, tier)
    backend = _get_backend(prov)
    logger.info("call_llm: provider=%s model=%s tier=%s bytes=%d",
                prov, model, tier, len(user or ""))
    return backend.call(user=user, system=system, model=model, timeout=timeout)


def health_check(provider: Optional[str] = None) -> Dict[str, Any]:
    """Quick sanity: does the selected provider have what it needs?"""
    prov = resolve_provider(provider)
    cfg = _load_config()
    pcfg = cfg.get("providers", {}).get(prov, {})
    missing = [e for e in pcfg.get("requires_env", []) if not os.environ.get(e)]
    result = {
        "provider": prov,
        "missing_env": missing,
        "ready": not missing,
    }
    if prov == "claude":
        try:
            from src.core.subprocess_launcher import CLAUDE_BIN
            result["claude_bin"] = CLAUDE_BIN or "(not found)"
            result["ready"] = bool(CLAUDE_BIN)
        except Exception:
            result["ready"] = False
    return result


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["health", "call"])
    ap.add_argument("--provider", type=str)
    ap.add_argument("--tier", type=str, default="cheap_fast")
    ap.add_argument("--user", type=str, default="say 'hello' as a single word")
    ap.add_argument("--system", type=str, default="")
    args = ap.parse_args()
    if args.cmd == "health":
        print(json.dumps(health_check(args.provider), indent=2))
    elif args.cmd == "call":
        try:
            r = call_llm(user=args.user, system=args.system, tier=args.tier, provider=args.provider)
            print(r)
        except LLMError as e:
            print(f"ERROR: {e}")
