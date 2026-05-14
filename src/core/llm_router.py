"""
LLM Router -- Claude CLI backend for memexa (v6.0).

Replaces Kimi/Moonshot API with `claude -p` subprocess calls.
Claude Max subscription = unlimited usage, zero API cost.

Migration (2026-04-03):
  Kimi moonshot-v1-* → claude -p (Opus via Max subscription)
  Budget management removed (Max = unlimited)
  Connection pool removed (single provider)

Backward-compatible interface: TaskType, call(), acall(), get_stats()
"""

import asyncio
import logging
import shutil
import subprocess
import threading
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class BudgetExceededError(Exception):
    """Kept for backward compat — never raised with Max subscription."""


class TaskType(Enum):
    CHAT = "chat"
    SUMMARY = "summary"
    CODE_REVIEW = "code_review"
    STRUCTURED_EXTRACT = "structured_extract"
    DEEP_ANALYSIS = "deep_analysis"
    EMBEDDING = "embedding"


class LLMRouter:
    """Routes LLM requests through Claude CLI (Max subscription).

    All task types use `claude -p` — no model selection needed.
    The CLI automatically uses the model configured in the user's subscription.
    """

    # Timeout per call (seconds)
    _CALL_TIMEOUT = 120
    # Default model for claude -p (sonnet = cost-effective, sufficient for review)
    _DEFAULT_MODEL = "sonnet"

    def __init__(self, config=None):
        # Unified CLI resolution via subprocess_launcher (allowlist + cache).
        # Lazy-imported to avoid circular imports at module load.
        from src.core.subprocess_launcher import CLAUDE_BIN as _BIN
        # Fallback to bare "claude" only if resolver returned None so existing
        # tests that monkeypatch subprocess still work; real invocations fail
        # fast with FileNotFoundError caught by callers.
        self._claude_bin = _BIN or "claude"
        self._call_count: int = 0
        self._error_count: int = 0
        self._model_usage: Dict[str, int] = {}
        self._semaphore: Optional[asyncio.Semaphore] = None
        logger.info("LLM Router: Claude CLI backend (model=%s)", self._DEFAULT_MODEL)

    # ------------------------------------------------------------------
    # Public API (backward-compatible)
    # ------------------------------------------------------------------

    def get_client(self, provider: str = "claude"):
        """Backward compat — returns self (router handles calls directly)."""
        return self

    def route(self, task_type: TaskType) -> str:
        """Return model identifier."""
        return f"claude-{self._DEFAULT_MODEL}"

    def route_by_complexity(self, task_type: TaskType, code: str = "") -> str:
        """Backward compat — always routes to Claude."""
        return f"claude-{self._DEFAULT_MODEL}"

    def estimate_complexity(self, code: str) -> str:
        """Estimate code complexity (kept for backward compat)."""
        lines_count = code.count('\n')
        classes = code.count('class ')
        functions = code.count('def ')
        if lines_count > 500 or classes > 5 or functions > 15:
            return 'complex'
        elif lines_count > 100 or classes > 2 or functions > 5:
            return 'moderate'
        return 'simple'

    def call(
        self,
        task_type: TaskType,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 2048,
        response_format=None,
        provider: str = "claude",
    ) -> str:
        """Call Claude via CLI subprocess.

        Args:
            task_type: determines logging context (model selection is automatic)
            messages: chat messages (user content extracted for prompt)
            temperature: ignored (Claude CLI doesn't expose this)
            max_tokens: max output tokens (passed via --max-turns 1)
            response_format: if {"type": "json_object"}, appends JSON instruction
            provider: ignored (always uses Claude)

        Returns:
            Response content string
        """
        # Build prompt from messages
        parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                parts.insert(0, f"[System instruction]: {content}")
            elif role == "user":
                parts.append(content)
            elif role == "assistant":
                parts.append(f"[Previous assistant response]: {content}")
        prompt = "\n\n".join(parts)

        # If JSON output requested, append instruction
        if response_format and response_format.get("type") == "json_object":
            prompt += "\n\nIMPORTANT: Return ONLY valid JSON, no other text."

        try:
            self._call_count += 1
            result = subprocess.run(
                [self._claude_bin, "-p", "--model", self._DEFAULT_MODEL,
                 "--output-format", "text"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._CALL_TIMEOUT,
                encoding="utf-8",
                errors="replace",
            )

            if result.returncode != 0:
                stderr = result.stderr.strip()[:200]
                self._error_count += 1
                raise RuntimeError(f"claude -p failed (rc={result.returncode}): {stderr}")

            content = result.stdout.strip()
            model_label = f"claude-{self._DEFAULT_MODEL}"
            self._model_usage[model_label] = self._model_usage.get(model_label, 0) + 1

            logger.debug(
                "LLM call OK (task=%s, len=%d chars)",
                task_type.value, len(content),
            )
            return content

        except subprocess.TimeoutExpired:
            self._error_count += 1
            raise RuntimeError(f"claude -p timed out after {self._CALL_TIMEOUT}s")
        except FileNotFoundError:
            self._error_count += 1
            raise RuntimeError(
                "claude CLI not found. Install: npm install -g @anthropic-ai/claude-code"
            )

    async def acall(
        self,
        task_type: TaskType,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 2048,
        response_format=None,
        provider: str = "claude",
    ) -> str:
        """Async wrapper — runs call() in thread executor."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(3)
        async with self._semaphore:
            return await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self.call(
                    task_type, messages, temperature, max_tokens,
                    response_format, provider,
                ),
            )

    # ------------------------------------------------------------------
    # Budget management (no-op with Max subscription)
    # ------------------------------------------------------------------

    def set_budget(self, limit: float) -> None:
        """No-op — Max subscription is unlimited."""
        logger.debug("LLM Router: set_budget(%.2f) ignored (Max subscription)", limit)

    def check_budget(self) -> bool:
        """Always returns True — Max subscription is unlimited."""
        return True

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @property
    def total_cost(self) -> float:
        return 0.0  # Max subscription = zero marginal cost

    def reset_cost(self):
        pass

    def get_stats(self) -> dict:
        return {
            "provider": "claude-cli",
            "subscription": "max",
            "total_cost": 0.0,
            "call_count": self._call_count,
            "error_count": self._error_count,
            "model_usage": dict(self._model_usage),
            "budget_limit": 0,
            "budget_remaining": None,
        }


# Singleton
_router = None
_router_lock = threading.Lock()


def get_router() -> LLMRouter:
    global _router
    if _router is None:
        with _router_lock:
            if _router is None:
                _router = LLMRouter()
    return _router
