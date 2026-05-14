"""TU-3 (2026-04-22): default-safe filesystem helpers.

Reusable safety predicates for `unlink` and similar path-mutating ops.
Centralizes the realpath+containment pattern that was duplicated (and
almost forgotten) in hook_posttool_agent_complete.py.

Motivation: security-reviewer f1ce71f flagged 2 HIGH findings (symlink-
redirected unlink in _read_ts_file and _gc_orphan_spawn_files). Rather
than rewrite the guard each time, callers can now import `safe_unlink`
and `is_safe_child` from here. Enforcement test (TU-4) prevents new
code from using raw `.unlink()` without going through this module.

Usage:
    from src.core._safe_fs import safe_unlink, is_safe_child
    if safe_unlink(target_path, base_dir):
        # deleted, target was a direct regular child of base_dir
        ...
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]


def is_safe_child(path: PathLike, base_dir: PathLike) -> bool:
    """Return True iff `path` is a direct, non-symlink, regular-file child
    of `base_dir`.

    SEC-R1-1 HIGH fix (2026-04-22): TOCTOU race between `p.is_symlink()`
    and `p.resolve(strict=True)` closed by using a single `os.lstat()`
    call to inspect path type + existence atomically. If the file is a
    symlink, S_ISLNK(lstat.st_mode) is true; if regular, S_ISREG is true.
    No second syscall gap for attacker to swap.

    Returns False on any exception (file missing, permission denied,
    symlink detected, outside base) — fail-closed contract.
    """
    import os as _os
    import stat as _stat
    try:
        p = Path(path)
        # Single syscall: lstat does NOT follow symlinks. If the target
        # is replaced with a symlink between this call and later use,
        # any subsequent resolve/open would also reject — but critically
        # we return False immediately if lstat shows non-regular.
        lst = _os.lstat(str(p))
        mode = lst.st_mode
        if _stat.S_ISLNK(mode):
            return False
        if not _stat.S_ISREG(mode):
            return False
        # LOG-R1 HIGH-2 fix (2026-04-22): base_dir may not exist on first
        # call (callers who forget to mkdir first would silently get
        # False). Use strict=False then explicitly check is_dir. API
        # becomes robust for future callers.
        real_path = p.resolve(strict=False)
        real_base = Path(base_dir).resolve(strict=False)
        if not real_base.is_dir():
            return False
        if not real_path.is_file():
            return False
    except Exception:
        return False
    try:
        real_path.relative_to(real_base)
    except ValueError:
        return False
    if real_path.parent != real_base:
        return False
    return True


def safe_unlink(path: PathLike, base_dir: PathLike) -> bool:
    """Unlink `path` iff it passes `is_safe_child(path, base_dir)`.

    Does NOT follow symlinks: if `path` is a symlink inside base_dir but
    points outside, this function refuses to unlink. (Use `safe_unlink_symlink`
    if you explicitly want to remove a link.)

    Args:
        path: target to remove
        base_dir: allowed parent directory

    Returns:
        True iff the file was successfully unlinked.
        False on any safety-check or OS failure — fail-soft.
    """
    try:
        p = Path(path)
        if not is_safe_child(p, base_dir):
            return False
        p.unlink()  # safe_fs_exempt: guarded above by is_safe_child
        return True
    except Exception as e:
        logger.debug("safe_unlink(%s) failed: %s", path, e)
        return False


def safe_unlink_symlink(link_path: PathLike, base_dir: PathLike) -> bool:
    """Variant for unlinking a symlink ITSELF (not its target).

    Used by GC sweeps that want to remove stale orphan links found inside
    `base_dir`. The link itself is removed via `path.unlink()` which, in
    Python pathlib, removes the link not its target. Still gated on
    base_dir containment to prevent traversal.

    SEC-R1-MED-2 fix (2026-04-22): previously used `.absolute().parent
    .resolve()` which could be bypassed by crafted `../base_dir/link`
    paths (`.absolute()` normalizes `..` components silently). Fix: use
    `p.resolve(strict=True, ...).parent` on the LINK FILE's own
    inode, but since resolve() follows symlinks, we instead resolve the
    parent dir directly then combine with link name.

    Returns True iff the symlink was removed.
    """
    import os as _os
    import stat as _stat
    try:
        p = Path(link_path)
        # Atomic type check: must be a symlink (lstat does NOT follow).
        lst = _os.lstat(str(p))
        if not _stat.S_ISLNK(lst.st_mode):
            return False
        # Containment check: resolve the PARENT directory only (so we
        # don't follow the symlink itself). This correctly rejects
        # `../base_dir/link` because the resolved parent would not be
        # under base_dir.
        real_base = Path(base_dir).resolve(strict=True)
        # Parent dir, strict (must exist)
        parent_real = p.parent.resolve(strict=True)
        if parent_real != real_base:
            return False
        p.unlink()  # safe_fs_exempt: removes symlink itself, not target
        return True
    except Exception as e:
        logger.debug("safe_unlink_symlink(%s) failed: %s", link_path, e)
        return False
