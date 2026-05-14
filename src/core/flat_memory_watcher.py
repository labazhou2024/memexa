"""flat_memory_watcher.py -- Detect flat memory/*.md changes via mtime+sha.

Light-weight incremental tracker. Pure stdlib. No LLM calls.

Design:
- Records last-seen (path, mtime, sha256) in data/flat_memory_state.json.
- changed_files() returns files whose mtime OR sha256 differs from snapshot.
- mark_seen(files) commits the new state (idempotent).
- No side effects beyond writing the state file. Caller decides what to do
  with the change list (migrate, re-extract, notify).

Why both mtime AND sha:
- mtime alone: tricks by git checkout / cp -p can reset mtime without content change
- sha alone: slow to compute on every check for 75+ files
- Use mtime as cheap first filter, sha as tiebreaker

Intended integration (NOT wired yet, flag=0):
  session_reflector Stop hook -> flat_memory_watcher.changed_files()
  -> if any, enqueue reflection targets -> after reflection, mark_seen()
"""
from __future__ import annotations


import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from src.core._path_resolver import memory_dir

_MEMORY_DIR = (
    memory_dir()
)
_DATA_DIR = Path(__file__).parent.parent / "data"
_STATE_FILE = _DATA_DIR / "flat_memory_state.json"


@dataclass
class FileSig:
    mtime: float
    sha: str
    size: int


def _sha_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _load_state() -> Dict[str, FileSig]:
    if not _STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: Dict[str, FileSig] = {}
    for k, v in raw.items():
        try:
            out[k] = FileSig(**v)
        except TypeError:
            continue
    return out


def _save_state(state: Dict[str, FileSig]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _STATE_FILE.with_suffix(".tmp")
    data = {k: asdict(v) for k, v in state.items()}
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _STATE_FILE)


def _scan_current(memory_dir: Optional[Path] = None) -> Dict[str, FileSig]:
    mdir = memory_dir or _MEMORY_DIR
    if not mdir.exists():
        return {}
    current: Dict[str, FileSig] = {}
    for p in sorted(mdir.glob("*.md")):
        st = p.stat()
        # Cheap check: mtime+size. sha only if we need to be sure.
        current[str(p)] = FileSig(mtime=st.st_mtime, sha="", size=st.st_size)
    return current


def _fill_sha_if_needed(current: Dict[str, FileSig],
                       prior: Dict[str, FileSig]) -> None:
    """Compute sha only for files whose (mtime, size) doesn't match prior.

    Avoids sha for ~95% of files on a typical session.
    """
    for path, sig in current.items():
        old = prior.get(path)
        if old is None or old.mtime != sig.mtime or old.size != sig.size:
            sig.sha = _sha_of(Path(path))
        else:
            sig.sha = old.sha  # reuse


def changed_files(memory_dir: Optional[Path] = None
                  ) -> Tuple[List[str], List[str], List[str]]:
    """Return (added, modified, removed) relative to last mark_seen().

    "modified" = mtime/size changed AND sha changed.
    """
    prior = _load_state()
    current = _scan_current(memory_dir)
    _fill_sha_if_needed(current, prior)

    added, modified = [], []
    for path, sig in current.items():
        old = prior.get(path)
        if old is None:
            added.append(path)
        elif old.sha != sig.sha:
            modified.append(path)
    removed = [p for p in prior if p not in current]
    return added, modified, removed


def mark_seen(memory_dir: Optional[Path] = None) -> Dict[str, FileSig]:
    """Commit current scan as the new baseline."""
    prior = _load_state()
    current = _scan_current(memory_dir)
    _fill_sha_if_needed(current, prior)
    _save_state(current)
    return current


def marked_seen(
    file_paths: List[str],
    memory_dir: Optional[Path] = None,
) -> Dict[str, FileSig]:
    """[AC-4 2026-04-19] Partial snapshot update for reflector incremental wiring.

    After session_reflector processes a subset of changed files, it calls
    ``marked_seen(file_paths)`` to update the sha snapshot ONLY for those files,
    leaving unprocessed files in the prior state so they remain "modified" on
    the next ``changed_files()`` call.

    Idempotent: calling with the same file_paths twice yields identical state.

    Behavior:
      - For each path in file_paths that exists under memory_dir, update its
        FileSig to current (mtime, size, sha).
      - If a path in file_paths no longer exists, remove it from state.
      - Paths NOT in file_paths are preserved from prior state.
      - Paths not under memory_dir are silently skipped (defense against
        cross-tree writes).

    Args:
        file_paths: list of absolute path strings (as returned by changed_files()).
        memory_dir: override for tests.

    Returns:
        The updated full state dict (for caller introspection).
    """
    prior = _load_state()
    # [LOG-R1-017 2026-04-20] Previously: if _MEMORY_DIR didn't exist, `mdir`
    # was the unresolved Path, and every subsequent `p.relative_to(mdir)` call
    # raised ValueError, silently dropping every target. That silently bypassed
    # AC-4 (incremental memory snapshot) whenever memory/ was first created.
    # Now: ensure mdir exists (create if missing) so relative_to works. Callers
    # that legitimately pass file_paths when memory_dir doesn't exist get a
    # directory created rather than a silent no-op.
    raw_mdir = memory_dir or _MEMORY_DIR
    try:
        raw_mdir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        mdir = raw_mdir.resolve()
    except (OSError, ValueError):
        mdir = raw_mdir
    # Normalize requested paths to absolute strings; filter to memory_dir scope
    targets: List[Path] = []
    for raw in file_paths or []:
        try:
            p = Path(raw).resolve()
        except (OSError, ValueError):
            continue
        try:
            p.relative_to(mdir)
        except ValueError:
            continue
        targets.append(p)

    new_state: Dict[str, FileSig] = dict(prior)  # start from prior snapshot
    for p in targets:
        key = str(p)
        if not p.exists():
            # file was deleted between changed_files() and marked_seen()
            new_state.pop(key, None)
            continue
        st = p.stat()
        new_state[key] = FileSig(
            mtime=st.st_mtime,
            sha=_sha_of(p),
            size=st.st_size,
        )
    _save_state(new_state)
    return new_state


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check", "mark"],
                    help="check: report changes; mark: commit snapshot")
    args = ap.parse_args()

    if args.cmd == "check":
        added, modified, removed = changed_files()
        print(f"added    : {len(added)}")
        for p in added[:10]: print(f"  + {Path(p).name}")
        print(f"modified : {len(modified)}")
        for p in modified[:10]: print(f"  ~ {Path(p).name}")
        print(f"removed  : {len(removed)}")
        for p in removed[:10]: print(f"  - {Path(p).name}")
    elif args.cmd == "mark":
        state = mark_seen()
        print(f"baseline: {len(state)} files snapshotted -> {_STATE_FILE}")


if __name__ == "__main__":
    main()
