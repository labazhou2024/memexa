# Migrating legacy path placeholders to `_path_resolver`

**English** · [中文](path_resolver.zh.md)

> Background: during the initial sanitisation pass we replaced literal
> Windows paths (`C:\Users\<name>\OneDrive\...\claude workspace\...`)
> with the template tokens `<USERPROFILE>` and `<WORKSPACE_ID>` so the
> code would not ship with anyone's personal path encoded in it. Some
> modules still contain these placeholder tokens — they are tagged with
> a `TODO(memgraph-oss)` comment near the top of the file.
>
> This document is the recipe for replacing those placeholders with
> calls to `src.core._path_resolver`.

## The shape of the problem

A typical legacy file looks like:

```python
# src/core/hard_rule_audit.py
PROJECT_PROJECTS_PATTERN = (
    "<USERPROFILE>/.claude/projects/"
    "<WORKSPACE_ID>/memory"
)
```

The intent of the original code was *"the canonical memory directory
under the user's `.claude/projects/<workspace-id>/` tree."*

After migration:

```python
from pathlib import Path
from src.core._path_resolver import workspace_root

# workspace_root() returns Path(~/.claude/projects/<workspace-id>) by
# default; users can override via MEMEXA_WORKSPACE_ROOT.
MEMORY_DIR = workspace_root() / "memory"
```

## Step-by-step

1. Open the file, locate the `TODO(memgraph-oss)` marker.
2. Identify every line that uses `<USERPROFILE>` or `<WORKSPACE_ID>`.
3. Classify the usage:
   - **Pattern A — path construction**: replace the literal string with
     a `workspace_root() / "subpath"` expression. Add the import if
     it is not already there.
   - **Pattern B — regex / glob pattern**: convert the string to a
     compiled `re.Pattern` that allows for any user prefix.
   - **Pattern C — display only**: replace with a more honest
     `<your-workspace-root>` placeholder if the string is ultimately
     printed to the user.
4. Remove the `TODO(memgraph-oss)` marker.
5. Add a unit test under `tests/unit/test_<modname>.py` that imports
   the module with `MEMEXA_WORKSPACE_ROOT` set to a temp dir and
   asserts the resolved path makes sense.

## Pattern A — path construction

Before:

```python
DEFAULT_BANK_PATH = "<USERPROFILE>/.claude/data/audit_corpus.jsonl"
```

After:

```python
from src.core._path_resolver import workspace_root

DEFAULT_BANK_PATH = workspace_root().parent / ".claude" / "data" / "audit_corpus.jsonl"
```

Convenience: `_path_resolver` already exposes `audit_corpus_path()`
for this specific file, so prefer:

```python
from src.core._path_resolver import audit_corpus_path

DEFAULT_BANK_PATH = audit_corpus_path()
```

## Pattern B — regex / glob pattern

Before:

```python
PROJECT_PROJECTS_PATTERN = (
    "<USERPROFILE>/.claude/projects/"
    "<WORKSPACE_ID>/memory"
)
```

After:

```python
import re
PROJECT_PROJECTS_PATTERN = re.compile(
    r".claude[\\/]projects[\\/][^\\/]+[\\/]memory(?:[\\/]|$)"
)
```

## Pattern C — display only

Before:

```python
ERROR_MSG = "expected file under <USERPROFILE>/.claude/projects"
```

After:

```python
ERROR_MSG = "expected file under <your-claude-projects-root>"
```

## Checklist before deleting the TODO

- [ ] The module imports `workspace_root` (or uses one of the exported
  convenience functions).
- [ ] Every `<USERPROFILE>` and `<WORKSPACE_ID>` in the file is gone or
  intentionally part of a user-facing message string.
- [ ] A test passes the module through a temp workspace.
- [ ] `make pii-scan` still reports zero hits on this file.
