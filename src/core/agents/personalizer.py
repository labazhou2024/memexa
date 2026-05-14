"""
Personalizer Agent - Dynamic Prompt Adjustment (STUB)

Original implementation depended on knowledge_base + knowledge_injector
(deleted in CC-Native migration). Retained as stub for import compatibility.
Functionality replaced by Claude Code auto-memory and semantic_memory patterns.
"""

import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class Personalizer:
    """Stub — personalization now handled by CC auto-memory + CLAUDE.md."""

    def build_context_prompt(self) -> str:
        return ""

    def personalize_prompt(self, base_prompt: str, task_type: str = None) -> str:
        return base_prompt

    def get_user_preferences(self) -> Dict[str, Any]:
        return {}
