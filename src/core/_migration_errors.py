"""TU-α4 (2026-04-21): migration-specific exceptions.

MigrationAmbiguousError: raised by migrate_entity_kind when the proposed
dry-run distribution has too many fall-through "other" classifications,
indicating the classifier can't do the job without hand-labeling. This
prevents a blind --apply from silently producing null→other rewrites
that appear successful but add no information.
"""
from __future__ import annotations

from typing import List, Tuple


class MigrationAmbiguousError(Exception):
    """Raised when proposed migration has too-high ambiguity.

    Attributes:
      other_count: rows classified as "other" fallback
      total_scanned: total rows in migration pass
      ratio: other_count / total_scanned
      top_unlabeled: list of (canon, raw_forms) awaiting hand label
    """

    def __init__(
        self,
        other_count: int,
        total_scanned: int,
        top_unlabeled: List[Tuple[str, list]],
        ratio_threshold: float = 0.5,
    ):
        self.other_count = other_count
        self.total_scanned = total_scanned
        self.ratio = (
            other_count / total_scanned if total_scanned > 0 else 0.0
        )
        self.top_unlabeled = top_unlabeled
        self.ratio_threshold = ratio_threshold
        super().__init__(
            f"MigrationAmbiguousError: other/total={other_count}/"
            f"{total_scanned} = {self.ratio:.2%} exceeds threshold "
            f"{ratio_threshold:.0%}. Hand-label required before --apply. "
            f"Top unlabeled: {top_unlabeled[:5]}"
        )
