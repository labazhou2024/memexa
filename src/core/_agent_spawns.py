"""LOG-R1 MED-2 fix (2026-04-22): single source of truth for agent_spawns dir.

Previously `_agent_spawns_dir()` was copy-pasted in hook_pretool_agent.py
AND hook_posttool_agent_complete.py. Any one-sided future change would
silently break the spawn-ts handshake (pretool writes to one dir,
posttool reads from another). Centralized here.

Both hooks import from this module.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def agent_spawns_dir() -> Path:
    """Return the agent_spawns directory, creating if missing.

    Respects `MEMEX_AGENT_SPAWNS_DIR` env var IFF its resolved path is
    under workspace OR system tempdir (SEC-R1-2 HIGH fix). Env values
    outside these roots are silently ignored and the default is used.

    Default = `<workspace>/.claude/harness/agent_spawns/` where workspace
    is this file's parent 4 levels up (memex/memex/core/ → workspace).
    """
    # Default = project_root/.claude/harness/agent_spawns
    default = (
        Path(__file__).resolve().parent.parent.parent.parent
        / ".claude" / "harness" / "agent_spawns"
    )
    env_override = os.environ.get("MEMEX_AGENT_SPAWNS_DIR")
    if env_override:
        try:
            override_path = Path(env_override).resolve(strict=False)
            workspace_root = default.parent.parent.resolve(strict=False)
            tempdir_root = Path(tempfile.gettempdir()).resolve(strict=False)
            ok = False
            for root in (workspace_root, tempdir_root):
                try:
                    override_path.relative_to(root)
                    ok = True
                    break
                except ValueError:
                    continue
            d = override_path if ok else default
        except Exception:
            d = default
    else:
        d = default
    d.mkdir(parents=True, exist_ok=True)
    return d
