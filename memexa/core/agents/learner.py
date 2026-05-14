"""
Learner Agent - Behavioral Learning (STUB)

Original implementation depended on context_bus (deleted in CC-Native migration).
Retained as stub for import compatibility. Functionality replaced by
semantic_memory + feedback_collector in the evolution stack.
"""

import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class Learner:
    """Stub — real learning now handled by semantic_memory + feedback_collector."""

    async def record_interaction(self, output: str, user_action: str, context: Dict = None):
        context = context or {}
        logger.debug("Learner.record_interaction: stub (use feedback_collector)")

    async def get_learning_stats(self) -> Dict[str, Any]:
        return {"total_interactions": 0, "note": "stub — see evolution_metrics"}
