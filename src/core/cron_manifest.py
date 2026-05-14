"""cron_manifest.py — YAML manifest SoT loader + pydantic validation (R-5).

Loads data/cron_manifest.yaml, validates against JSON Schema +
pydantic models, provides lookup helpers.

sec-iter1-5: ssh_host field restricted to no shell metachars via both
JSON Schema pattern and pydantic validator.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import List, Optional

try:
    from pydantic import BaseModel, field_validator, model_validator
    _PYDANTIC_V2 = True
except ImportError:
    try:
        from pydantic import BaseModel, validator as field_validator
        _PYDANTIC_V2 = False
    except ImportError:
        BaseModel = object  # type: ignore
        _PYDANTIC_V2 = False

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

try:
    import jsonschema as _jsonschema
    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_DEFAULT_MANIFEST = _REPO / "data" / "cron_manifest.yaml"
_DEFAULT_SCHEMA = _REPO / "data" / "cron_manifest_schema.json"

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SSH_HOST_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
_SCHEDULE_RE = re.compile(r"^(daily \d{2}:\d{2}|every \d+h|every \d+min|@\w+)$")


class CronManifestEntry(BaseModel if BaseModel is not object else object):  # type: ignore
    """One driver entry in cron_manifest.yaml."""
    id: str
    host: str  # "win" | "mac"
    schedule: str
    driver_module: str
    args: List[str] = []
    incremental_args: List[str]
    description: str = ""
    ssh_host: Optional[str] = None
    # 2026-05-12: 当 driver 有自己的 schtask (e.g. AudioIngest6h), 不要让
    # GraphMaintenance6h `run-incremental --all` 也跑它,否则双跑 + 拖长总耗时.
    # 设 True 时 cron_orchestrator.run_all_for_host() 跳过.
    skip_in_orchestrator: bool = False

    if _PYDANTIC_V2:
        @field_validator("id")
        @classmethod
        def id_no_metachars(cls, v: str) -> str:
            if not _ID_RE.match(v):
                raise ValueError(f"id must match ^[a-z][a-z0-9_]*$ got {v!r}")
            return v

        @field_validator("host")
        @classmethod
        def host_enum(cls, v: str) -> str:
            if v not in ("win", "mac"):
                raise ValueError(f"host must be win|mac got {v!r}")
            return v

        @field_validator("ssh_host")
        @classmethod
        def ssh_host_no_metachars(cls, v: Optional[str]) -> Optional[str]:
            if v is not None and not _SSH_HOST_RE.match(v):
                raise ValueError(
                    f"ssh_host must match ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ got {v!r}"
                )
            return v


class SchtaskEntry(BaseModel if BaseModel is not object else object):  # type: ignore
    """One schtasks entry in the manifest."""
    id: str
    host: str
    task_path: str
    schedule: str
    description: str = ""


class CronManifest(BaseModel if BaseModel is not object else object):  # type: ignore
    """Top-level manifest container."""
    version: int
    drivers: List[CronManifestEntry]
    schtasks: List[SchtaskEntry] = []

    def get_drivers_by_host(self, host: str) -> List[CronManifestEntry]:
        """Return all driver entries matching host (win|mac)."""
        return [d for d in self.drivers if d.host == host]

    def get_driver_by_id(self, driver_id: str) -> Optional[CronManifestEntry]:
        """Return driver entry by id, or None if not found."""
        for d in self.drivers:
            if d.id == driver_id:
                return d
        return None


def _load_yaml(path: Path) -> dict:
    """Load YAML file; fallback to simple line parser if PyYAML not installed."""
    if _HAS_YAML:
        with path.open(encoding="utf-8") as f:
            return _yaml.safe_load(f)
    # Minimal fallback: convert to JSON-compatible using a simplistic approach
    raise ImportError(
        "PyYAML is required to load cron_manifest.yaml. "
        "Install with: pip install pyyaml"
    )


def _validate_schema(data: dict, schema_path: Path) -> None:
    """Validate dict against JSON Schema if jsonschema available."""
    if not _HAS_JSONSCHEMA:
        return  # Skip schema validation — import guard only
    if not schema_path.exists():
        return
    with schema_path.open(encoding="utf-8") as f:
        schema = json.load(f)
    _jsonschema.validate(instance=data, schema=schema)


def load(
    path: Optional[Path] = None,
    schema_path: Optional[Path] = None,
) -> CronManifest:
    """Load and validate cron manifest from YAML.

    Args:
        path: Path to YAML manifest (default: data/cron_manifest.yaml)
        schema_path: Path to JSON Schema (default: data/cron_manifest_schema.json)

    Returns:
        Validated CronManifest instance.

    Raises:
        FileNotFoundError: if manifest file not found
        ValueError: if validation fails
    """
    manifest_path = path or _DEFAULT_MANIFEST
    schema_file = schema_path or _DEFAULT_SCHEMA

    if not manifest_path.exists():
        raise FileNotFoundError(f"cron_manifest.yaml not found: {manifest_path}")

    raw = _load_yaml(manifest_path)

    # JSON Schema validation
    try:
        _validate_schema(raw, schema_file)
    except Exception as exc:
        raise ValueError(f"cron_manifest.yaml failed JSON Schema validation: {exc}") from exc

    # Pydantic model validation
    if BaseModel is object:
        # Fallback: build plain CronManifest-like object via dict
        return _build_without_pydantic(raw)

    try:
        # Build driver entries
        driver_list = []
        for d in raw.get("drivers", []):
            driver_list.append(CronManifestEntry(**d))
        schtask_list = []
        for s in raw.get("schtasks", []):
            schtask_list.append(SchtaskEntry(**s))
        return CronManifest(
            version=raw["version"],
            drivers=driver_list,
            schtasks=schtask_list,
        )
    except Exception as exc:
        raise ValueError(f"cron_manifest.yaml pydantic validation failed: {exc}") from exc


def _build_without_pydantic(raw: dict) -> "CronManifest":
    """Minimal dataclass-like builder when pydantic is absent."""
    import types

    class _Entry:
        def __init__(self, d: dict) -> None:
            self.id = d["id"]
            self.host = d["host"]
            self.schedule = d["schedule"]
            self.driver_module = d["driver_module"]
            self.args = d.get("args", [])
            self.incremental_args = d["incremental_args"]
            self.description = d.get("description", "")
            self.ssh_host = d.get("ssh_host")
            # Validate id
            if not _ID_RE.match(self.id):
                raise ValueError(f"id must match ^[a-z][a-z0-9_]*$ got {self.id!r}")
            if self.host not in ("win", "mac"):
                raise ValueError(f"host must be win|mac got {self.host!r}")
            if self.ssh_host and not _SSH_HOST_RE.match(self.ssh_host):
                raise ValueError(f"ssh_host has metachars: {self.ssh_host!r}")

    class _Schtask:
        def __init__(self, d: dict) -> None:
            self.id = d["id"]
            self.host = d["host"]
            self.task_path = d["task_path"]
            self.schedule = d["schedule"]
            self.description = d.get("description", "")

    class _Manifest:
        def __init__(self, version: int, drivers: list, schtasks: list) -> None:
            self.version = version
            self.drivers = drivers
            self.schtasks = schtasks

        def get_drivers_by_host(self, host: str) -> list:
            return [d for d in self.drivers if d.host == host]

        def get_driver_by_id(self, driver_id: str):
            for d in self.drivers:
                if d.id == driver_id:
                    return d
            return None

    drivers = [_Entry(d) for d in raw.get("drivers", [])]
    schtasks = [_Schtask(s) for s in raw.get("schtasks", [])]
    return _Manifest(version=raw["version"], drivers=drivers, schtasks=schtasks)


if __name__ == "__main__":
    # Quick validation CLI: python -m src.core.cron_manifest
    try:
        m = load()
        print(f"OK: version={m.version}, drivers={len(m.drivers)}, schtasks={len(m.schtasks)}")
        for d in m.drivers:
            print(f"  driver: {d.id}  host={d.host}  schedule={d.schedule!r}")
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
