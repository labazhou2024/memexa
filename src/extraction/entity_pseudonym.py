"""U8 (chat_to_graph plan_v3_FINAL §3): entity_pseudonym + vault_chat.bin escrow.

Group-chat members are pseudonymized as `person_<base32 16 chars>`. Real
display_name is encrypted in vault_chat.bin (Argon2id m=32MiB t=3 p=4 +
ChaCha20-Poly1305) and only revealed via CLI passphrase prompt with audit
trace event.

WeChat per-DB AES enc_keys are escrowed to the same vault for T10 ban-recovery
(separate namespace; no cross-leakage).

axis_anchor: [C:cli:entity_pseudonym], [C:schema:vault_chat_keys]
trace events: entity_pseudonym_minted, entity_pseudonym_revealed (audit),
              entity_pseudonym_locked, enc_keys_escrowed

Threat model:
  - Lost machine + plaintext disk read: vault opaque without passphrase
  - Adversarial caller in same Python process: locked-state cleared via lock_now()
  - Argon2id resistance: m=32MiB t=3 p=4 (RFC 9106 second recommended set)

NOT addressed:
  - Memory-resident attacks during unlock window (passphrase in RAM ~150ms)
  - Side-channel timing (KDF wall-clock varies by ~30%)
  - Multi-user vault (single CEO use case only)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

# Reuse same crypto primitives as browser_vault (don't import inside-package
# guarded modules; use libs directly with same parameters).
try:
    from argon2.low_level import Type as Argon2Type, hash_secret_raw
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
except ImportError as e:
    raise ImportError(
        "U8 entity_pseudonym requires argon2-cffi + cryptography. "
        f"Missing: {e}"
    ) from e

# Argon2id parameters: parity with memex.browser_vault._vault.kdf
KDF_M_KIB = 32768  # 32 MiB
KDF_T_COST = 3
KDF_P_PARALLEL = 4
KDF_KEY_LEN = 32  # ChaCha20-Poly1305 key length
NONCE_LEN = 12

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VAULT_PATH = REPO_ROOT / "data" / "vault_chat.bin"

# Magic header for file format detection
MAGIC = b"VC01"  # vault_chat v01


@dataclass
class _Maps:
    """In-memory representation of vault contents."""
    forward: Dict[str, str]   # name_hash → uuid
    reverse: Dict[str, bytes]  # uuid → encrypted display_name (base64-decoded raw)
    enc_keys: Dict[str, bytes]  # path_hash → encrypted aes_key
    salt: bytes


class _SessionCache:
    """Holds unlocked maps in memory; cleared on lock_now()."""
    def __init__(self) -> None:
        self.maps: Optional[_Maps] = None
        self.unlocked_at: Optional[float] = None
        self.passphrase: Optional[bytes] = None  # held only during unlock window

    def lock(self) -> None:
        # Best-effort scrub
        if self.passphrase is not None:
            self.passphrase = b"\x00" * len(self.passphrase)
        self.maps = None
        self.unlocked_at = None
        self.passphrase = None


_session = _SessionCache()


def _derive_key(passphrase: bytes, salt: bytes) -> bytes:
    """Argon2id KDF; same params as browser_vault."""
    if len(salt) < 8:
        raise ValueError("salt too short")
    return hash_secret_raw(
        secret=passphrase,
        salt=salt,
        time_cost=KDF_T_COST,
        memory_cost=KDF_M_KIB,
        parallelism=KDF_P_PARALLEL,
        hash_len=KDF_KEY_LEN,
        type=Argon2Type.ID,
    )


def _seal(plaintext: bytes, key: bytes, nonce: Optional[bytes] = None) -> bytes:
    """Returns nonce || ciphertext+tag (single blob for storage)."""
    nonce = nonce or os.urandom(NONCE_LEN)
    aead = ChaCha20Poly1305(key)
    return nonce + aead.encrypt(nonce, plaintext, None)


def _unseal(blob: bytes, key: bytes) -> bytes:
    """Reverse of _seal; raises InvalidTag on tamper or wrong key."""
    nonce = blob[:NONCE_LEN]
    ct = blob[NONCE_LEN:]
    aead = ChaCha20Poly1305(key)
    return aead.decrypt(nonce, ct, None)


def _hash_name(name: str) -> str:
    """Deterministic 16-hex-char hash of display_name (forward-map key)."""
    return hashlib.sha256(name.encode("utf-8", errors="replace")).hexdigest()[:16]


def _new_uuid() -> str:
    """Mint new pseudonym; ~80-bit entropy."""
    raw = secrets.token_bytes(10)
    encoded = base64.b32encode(raw).decode("ascii").rstrip("=").lower()
    return f"person_{encoded[:16]}"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _serialize_maps(maps: _Maps, key: bytes) -> bytes:
    """File layout: MAGIC (4B) || salt (16B) || sealed_payload."""
    payload = {
        "forward": maps.forward,
        "reverse": {k: base64.b64encode(v).decode("ascii") for k, v in maps.reverse.items()},
        "enc_keys": {k: base64.b64encode(v).decode("ascii") for k, v in maps.enc_keys.items()},
    }
    plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sealed = _seal(plaintext, key)
    return MAGIC + maps.salt + sealed


def _deserialize_maps(blob: bytes, passphrase: bytes) -> _Maps:
    """Parse vault file; raises ValueError on bad magic / tamper."""
    if len(blob) < 4 + 16 + NONCE_LEN + 16:  # magic + salt + nonce + min ct
        raise ValueError("vault file too short / corrupted")
    if blob[:4] != MAGIC:
        raise ValueError(f"bad magic: {blob[:4]!r}")
    salt = blob[4:20]
    sealed = blob[20:]
    key = _derive_key(passphrase, salt)
    plaintext = _unseal(sealed, key)
    payload = json.loads(plaintext.decode("utf-8"))
    return _Maps(
        forward=dict(payload.get("forward", {})),
        reverse={k: base64.b64decode(v) for k, v in payload.get("reverse", {}).items()},
        enc_keys={k: base64.b64decode(v) for k, v in payload.get("enc_keys", {}).items()},
        salt=salt,
    )


def _load_or_init(passphrase: bytes, vault_path: Optional[Path] = None) -> _Maps:
    """Load existing vault OR initialize empty one. Updates _session cache."""
    path = vault_path or DEFAULT_VAULT_PATH
    if path.exists():
        try:
            blob = path.read_bytes()
            maps = _deserialize_maps(blob, passphrase)
        except (ValueError, Exception):
            raise
    else:
        maps = _Maps(forward={}, reverse={}, enc_keys={}, salt=os.urandom(16))
    _session.maps = maps
    _session.unlocked_at = time.time()
    _session.passphrase = passphrase
    return maps


def _save(maps: _Maps, vault_path: Optional[Path] = None) -> None:
    """Re-derive key from session passphrase, re-seal, atomic write."""
    if _session.passphrase is None:
        raise RuntimeError("vault not unlocked; cannot save")
    key = _derive_key(_session.passphrase, maps.salt)
    blob = _serialize_maps(maps, key)
    _atomic_write(vault_path or DEFAULT_VAULT_PATH, blob)


def _emit_trace(event: str, payload: dict) -> None:
    try:
        from src.core.trace_sink import emit  # type: ignore
        emit(event, payload)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_or_mint_uuid(
    display_name: str,
    passphrase: Optional[bytes] = None,
    vault_path: Optional[Path] = None,
) -> str:
    """Return existing uuid OR mint new one for display_name.

    Required: passphrase ON FIRST CALL per session (to unlock or init vault).
    Subsequent calls in same session use _session.maps cache.

    Idempotent for same display_name (deterministic via name_hash key).
    """
    if not display_name or not isinstance(display_name, str):
        raise ValueError("display_name must be non-empty str")

    # Unlock vault if not already in session
    if _session.maps is None:
        if passphrase is None:
            raise RuntimeError(
                "vault locked; pass passphrase= on first call this session"
            )
        _load_or_init(passphrase, vault_path=vault_path)

    maps = _session.maps
    assert maps is not None

    name_hash = _hash_name(display_name)
    if name_hash in maps.forward:
        return maps.forward[name_hash]

    # Mint new
    uuid = _new_uuid()
    while uuid in maps.reverse:  # extremely rare collision
        uuid = _new_uuid()
    # Encrypt name for reverse map (separate per-record nonce)
    key = _derive_key(_session.passphrase, maps.salt)
    encrypted_name = _seal(display_name.encode("utf-8"), key)
    maps.forward[name_hash] = uuid
    maps.reverse[uuid] = encrypted_name
    _save(maps, vault_path=vault_path)
    _emit_trace("entity_pseudonym_minted", {
        "uuid_prefix": uuid[:14] + "...",  # don't log full uuid
        "name_hash": name_hash,
    })
    return uuid


def reveal_name(
    uuid: str,
    passphrase: bytes,
    vault_path: Optional[Path] = None,
    caller_tag: str = "cli",
) -> str:
    """Reverse-lookup display_name from uuid; passphrase required.

    Always emits audit trace `entity_pseudonym_revealed`. Returns "<unknown>"
    if uuid not in vault (NOT raise — protects callers).
    """
    if not uuid or not isinstance(uuid, str):
        raise ValueError("uuid must be non-empty str")
    if not passphrase or not isinstance(passphrase, bytes):
        raise ValueError("passphrase must be non-empty bytes")

    # Always re-derive (don't trust session if passphrase mismatch possible)
    path = vault_path or DEFAULT_VAULT_PATH
    if not path.exists():
        _emit_trace("entity_pseudonym_revealed", {
            "uuid_sha": hashlib.sha256(uuid.encode()).hexdigest()[:16],
            "caller_tag": caller_tag,
            "ts": time.time(),
            "result": "vault_missing",
        })
        return "<unknown>"

    blob = path.read_bytes()
    maps = _deserialize_maps(blob, passphrase)  # raises on wrong passphrase

    if uuid not in maps.reverse:
        # LG-iter1 MED fix: distinguish probe-attempt from legitimate reveal
        _emit_trace("entity_pseudonym_revealed", {
            "uuid_sha": hashlib.sha256(uuid.encode()).hexdigest()[:16],
            "caller_tag": caller_tag,
            "ts": time.time(),
            "result": "unknown_uuid",
        })
        return "<unknown>"

    _emit_trace("entity_pseudonym_revealed", {
        "uuid_sha": hashlib.sha256(uuid.encode()).hexdigest()[:16],
        "caller_tag": caller_tag,
        "ts": time.time(),
        "result": "ok",
    })
    key = _derive_key(passphrase, maps.salt)
    name_bytes = _unseal(maps.reverse[uuid], key)
    return name_bytes.decode("utf-8")


def lock_now() -> None:
    """Clear session cache; subsequent ops require passphrase again."""
    _session.lock()
    _emit_trace("entity_pseudonym_locked", {"ts": time.time()})


def status(vault_path: Optional[Path] = None) -> dict:
    """Non-secret status snapshot."""
    path = vault_path or DEFAULT_VAULT_PATH
    return {
        "vault_path": str(path),
        "vault_exists": path.exists(),
        "unlocked": _session.maps is not None,
        "unlocked_at": _session.unlocked_at,
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }


# ---------------------------------------------------------------------------
# WeChat enc_keys escrow (T10 ban-recovery)
# ---------------------------------------------------------------------------


def escrow_enc_keys(
    enc_keys_map: Dict[str, bytes],
    passphrase: bytes,
    vault_path: Optional[Path] = None,
) -> int:
    """Store WeChat per-DB AES keys to vault namespace `enc_keys`.

    Returns count of keys escrowed. Idempotent — overwrites existing entries.
    """
    if not isinstance(enc_keys_map, dict):
        raise TypeError("enc_keys_map must be dict")
    if not enc_keys_map:
        return 0

    if _session.maps is None or _session.passphrase != passphrase:
        _load_or_init(passphrase, vault_path=vault_path)

    maps = _session.maps
    assert maps is not None

    key = _derive_key(passphrase, maps.salt)
    count = 0
    for db_path, aes_key in enc_keys_map.items():
        if not isinstance(aes_key, bytes):
            continue
        path_hash = hashlib.sha256(str(db_path).encode("utf-8")).hexdigest()[:16]
        encrypted_key = _seal(aes_key, key)
        maps.enc_keys[path_hash] = encrypted_key
        count += 1
    _save(maps, vault_path=vault_path)
    _emit_trace("enc_keys_escrowed", {"count": count})
    return count


def recover_enc_keys(
    passphrase: bytes,
    vault_path: Optional[Path] = None,
) -> Dict[str, bytes]:
    """Decrypt all escrowed enc_keys; returns {path_hash: aes_key}.

    NOTE: returns hash-keyed map (not original db_paths) since paths are
    not stored in plaintext for privacy. Callers must re-derive path_hash
    via sha256(path)[:16] to match.
    """
    path = vault_path or DEFAULT_VAULT_PATH
    if not path.exists():
        return {}
    blob = path.read_bytes()
    maps = _deserialize_maps(blob, passphrase)  # raises on wrong passphrase
    key = _derive_key(passphrase, maps.salt)
    return {h: _unseal(enc, key) for h, enc in maps.enc_keys.items()}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_passphrase(prompt: str = "Vault passphrase: ") -> bytes:
    """getpass; defensive against non-tty (CI). Returns bytes."""
    try:
        import getpass
        s = getpass.getpass(prompt)
    except Exception as e:
        print(f"ERROR: passphrase prompt unavailable: {e}", file=sys.stderr)
        sys.exit(2)
    if not s:
        print("ERROR: empty passphrase rejected", file=sys.stderr)
        sys.exit(2)
    return s.encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="entity_pseudonym")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show vault status (no secret)")
    sub.add_parser("lock", help="clear session cache")
    p_reveal = sub.add_parser("reveal", help="reveal display_name for uuid")
    p_reveal.add_argument("uuid", help="person_<...> uuid")
    args = p.parse_args(argv)

    if args.cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "lock":
        lock_now()
        print("locked")
        return 0
    if args.cmd == "reveal":
        passphrase = _read_passphrase()
        try:
            name = reveal_name(args.uuid, passphrase, caller_tag="cli")
        except Exception as e:
            print(f"ERROR: reveal failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(name)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
