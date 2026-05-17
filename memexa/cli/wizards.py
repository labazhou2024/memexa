"""memexa CLI wizards -- interactive onboarding for first-time sources.

v0.1.1: introduces ``memexa init <source>`` and ``memexa ingest <source>``
flows so users do not have to hand-edit ``identity.yaml`` or know which
``python -m memexa.*`` module to run.

Currently shipped:

  - ``init_email_wizard``: 6-provider auto-detect IMAP onboarding.
    Writes to ``email.accounts.<name>`` block in ``~/.memexa/identity.yaml``.
  - ``init_wechat_wizard``: wraps the third-party WeChatMsg exporter on
    Windows (detects existing install, otherwise points user at the
    release page; not a full GUI hand-off in v0.1.1).
  - ``ingest_email``: drives ``email_history_fetcher`` for every
    configured account, prints progress.
  - ``ingest_wechat``: ingests a WeChatMsg export directory through
    ``v5_wechat_batch_builder``.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional


# -----------------------------------------------------------------------------
# Email provider auto-detect
# -----------------------------------------------------------------------------
#
# Each entry maps an email domain to:
#   (imap_host, imap_port, friendly_credential_name, help_url)
#
# friendly_credential_name is shown to the user so they know to grab a
# "授权码" (Chinese providers) or "App-Specific Password" (international)
# rather than their plain login password. help_url points at the
# provider's own page for generating that credential.
EMAIL_PROVIDERS: Dict[str, tuple] = {
    "gmail.com": (
        "imap.gmail.com", 993, "App password",
        "https://myaccount.google.com/apppasswords",
    ),
    "googlemail.com": (
        "imap.gmail.com", 993, "App password",
        "https://myaccount.google.com/apppasswords",
    ),
    "outlook.com": (
        "outlook.office365.com", 993, "App password",
        "https://account.live.com/proofs/AppPassword",
    ),
    "hotmail.com": (
        "outlook.office365.com", 993, "App password",
        "https://account.live.com/proofs/AppPassword",
    ),
    "live.com": (
        "outlook.office365.com", 993, "App password",
        "https://account.live.com/proofs/AppPassword",
    ),
    "icloud.com": (
        "imap.mail.me.com", 993, "App-Specific Password",
        "https://appleid.apple.com/account/manage",
    ),
    "qq.com": (
        "imap.qq.com", 993, "授权码 (Authorization code)",
        "https://service.mail.qq.com/detail/0/76",
    ),
    "foxmail.com": (
        "imap.qq.com", 993, "授权码 (Authorization code)",
        "https://service.mail.qq.com/detail/0/76",
    ),
    "163.com": (
        "imap.163.com", 993, "授权码 (Authorization code)",
        "https://help.mail.163.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac1",
    ),
    "126.com": (
        "imap.126.com", 993, "授权码 (Authorization code)",
        "https://help.mail.163.com",
    ),
    "yeah.net": (
        "imap.yeah.net", 993, "授权码 (Authorization code)",
        "https://help.mail.163.com",
    ),
    "sina.com": (
        "imap.sina.com", 993, "授权码 (Authorization code)",
        "https://mail.sina.com.cn",
    ),
    "mail.ustc.edu.cn": (
        # LIVE-verified 2026-05-17 on a USTC account:
        # imap.exmail.qq.com replies to TCP but rejects LOGIN; the
        # USTC-side reverse proxy mail.ustc.edu.cn accepts the same
        # 16-char auth code. Route USTC users here, not to the
        # generic Tencent Exmail host.
        "mail.ustc.edu.cn", 993,
        "Client auth code (16-char token, USTC Tencent Exmail reverse proxy)",
        "https://mail.ustc.edu.cn  (Settings -> Client -> IMAP/SMTP -> generate)",
    ),
    "exmail.qq.com": (
        # Generic corporate Tencent Exmail (non-USTC). For these,
        # imap.exmail.qq.com is the right host.
        "imap.exmail.qq.com", 993,
        "Client auth code (16-char token, Tencent Exmail)",
        "https://exmail.qq.com  (Settings -> Client -> IMAP/SMTP -> generate)",
    ),
}


def _detect_provider(email_addr: str) -> Optional[tuple]:
    """Return ``(host, port, credential_name, help_url)`` if domain known."""
    if "@" not in email_addr:
        return None
    domain = email_addr.split("@", 1)[1].strip().lower()
    if domain in EMAIL_PROVIDERS:
        return EMAIL_PROVIDERS[domain]
    # Heuristic: exmail.qq.com hosts most Chinese-corporate domains; try
    # known suffixes.
    if domain.endswith(".edu.cn") or domain in ("mail.ustc.edu.cn",):
        return EMAIL_PROVIDERS.get("mail.ustc.edu.cn")
    return None


def _resolve_config_dir() -> Path:
    return Path(
        os.environ.get("MEMEXA_CONFIG_DIR", str(Path.home() / ".memexa"))
    ).expanduser()


def _load_identity(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    import yaml  # type: ignore
    with path.open("r", encoding="utf-8") as fp:
        return yaml.safe_load(fp) or {}


def _save_identity(path: Path, data: Dict[str, Any]) -> None:
    import yaml  # type: ignore
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        yaml.safe_dump(data, fp, allow_unicode=True, sort_keys=False)
    tmp.replace(path)


def _prompt(question: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            v = input(f"  {question}{suffix}: ").strip()
        except EOFError:
            v = ""
        if v:
            return v
        if default is not None:
            return default
        print("  (required)")


# -----------------------------------------------------------------------------
# Email wizard
# -----------------------------------------------------------------------------

def init_email_wizard(args: argparse.Namespace) -> int:
    """Interactive ``memexa init email`` flow.

    Writes a new account into ``~/.memexa/identity.yaml`` under
    ``email.accounts.<name>``. Does not touch the env var (the user is
    instructed to export it themselves so the secret never lands in the
    YAML or in a shell history dump).
    """
    cfg_dir = _resolve_config_dir()
    identity_path = cfg_dir / "identity.yaml"
    data = _load_identity(identity_path)
    data.setdefault("email", {}).setdefault("accounts", {})

    print()
    print("memexa init email -- IMAP onboarding wizard")
    print("-" * 50)
    print(f"Config file: {identity_path}")
    print()

    email_addr = _prompt("Email address (alice@example.com)")
    detected = _detect_provider(email_addr)

    if detected:
        host_default, port_default, cred_name, help_url = detected
        print(f"  -> detected provider: {host_default}:{port_default}")
        print(f"  -> credential type:   {cred_name}")
        print(f"  -> get one here:      {help_url}")
    else:
        host_default = ""
        port_default = 993
        cred_name = "IMAP password (or app-specific password)"
        help_url = "your provider's help page"
        print("  (unknown provider -- you will need to provide host/port manually)")

    host = _prompt("IMAP host", host_default or None)
    port_str = _prompt("IMAP port", str(port_default))
    try:
        port = int(port_str)
    except ValueError:
        port = port_default

    account_name = _prompt(
        "Account label (used internally, no spaces)",
        email_addr.split("@", 1)[0].lower(),
    )
    pw_env_default = f"MEMEXA_IMAP_{account_name.upper()}_PASSWORD"
    pw_env = _prompt("Env var name for the password", pw_env_default)

    folders_in = _prompt("Folders (comma-separated)", "INBOX,Sent")
    folders = [f.strip() for f in folders_in.split(",") if f.strip()]

    since_days = _prompt("How many days back to fetch on first run", "90")
    try:
        since_days_int = int(since_days)
    except ValueError:
        since_days_int = 90

    data["email"]["accounts"][account_name] = {
        "host": host,
        "port": port,
        "user": email_addr,
        "password_env": pw_env,
        "folders": folders,
        "since_days": since_days_int,
    }
    _save_identity(identity_path, data)

    print()
    print(f"[ok] wrote account {account_name!r} -> {identity_path}")
    print()
    print("Next steps:")
    print(f"  1. Get your {cred_name} from {help_url}")
    print(f"  2. Export it:  export {pw_env}='<paste-it-here>'")
    print( "                 (Windows PowerShell:  $env:" + pw_env + "='<paste>')")
    print( "  3. Fetch:      memexa ingest email")
    print( "  4. Query:      memexa quick \"<question>\"")
    print()
    # Substring checks would be flagged by CodeQL
    # (py/incomplete-url-substring-sanitization). Use domain-suffix
    # match so an attacker can't sneak past with
    # `exmail.qq.com.evil.com`. The check is print-NOTE-only -- no
    # security boundary -- but tightening it keeps CodeQL clean.
    _email_domain = email_addr.split("@", 1)[-1].lower() if "@" in email_addr else ""
    _is_ustc = _email_domain == "mail.ustc.edu.cn"
    _is_exmail_host = host.lower().endswith(".exmail.qq.com") or host.lower() == "imap.exmail.qq.com"
    if _is_ustc or _is_exmail_host:
        print("NOTE: USTC mail and other Tencent Exmail accounts use")
        print("      imap.exmail.qq.com (NOT mail.ustc.edu.cn). The IMAP")
        print("      credential is a 16-char client auth code generated in")
        print("      the Exmail web console -- NOT your web login password.")
        print("      EAS / ActiveSync rejects the same code; only IMAP works.")
        print()
    return 0


# -----------------------------------------------------------------------------
# WeChat wizard (wraps third-party WeChatMsg)
# -----------------------------------------------------------------------------

WECHATMSG_RELEASE_URL = "https://github.com/LC044/WeChatMsg/releases/latest"


def _detect_wechatmsg() -> Optional[Path]:
    """Locate a WeChatMsg install. Windows-only; returns None elsewhere."""
    if sys.platform != "win32":
        return None
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "WeChatMsg" / "WeChatMsg.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "WeChatMsg" / "WeChatMsg.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "WeChatMsg" / "WeChatMsg.exe",
        Path.home() / "Downloads" / "WeChatMsg" / "WeChatMsg.exe",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def init_wechat_wizard(args: argparse.Namespace) -> int:
    """Wrap third-party WeChatMsg exporter onboarding.

    v0.1.1 scope: detect existing install, otherwise tell the user
    where to get one and where to drop the export. We do NOT auto-
    download the EXE -- the user has to grab it themselves (security:
    auto-downloading and running an EXE is more dangerous than just
    linking to it).
    """
    cfg_dir = _resolve_config_dir()
    identity_path = cfg_dir / "identity.yaml"

    print()
    print("memexa init wechat -- WeChatMsg export onboarding")
    print("-" * 50)

    if sys.platform != "win32":
        print()
        print("[skip] WeChat history is Windows-only today.")
        print()
        print("Why: every recommended exporter (WeChatMsg, wechatDataBackup,")
        print("     PyWxDump) reads the local Windows-WeChat SQLite DB. The")
        print("     macOS WeChat client uses a different (proprietary) format")
        print("     that has no open-source decoder.")
        print()
        print("If you have a Windows machine you can run WeChatMsg there,")
        print("export to JSON, copy the export directory to this machine,")
        print("then run `memexa ingest wechat --from <export-dir>`.")
        return 0

    found = _detect_wechatmsg()
    if found:
        print(f"[ok] found WeChatMsg at {found}")
    else:
        print("[info] WeChatMsg.exe not found in common locations.")
        print()
        print(f"  1. Download from {WECHATMSG_RELEASE_URL}")
        print( "  2. Install it (or unzip the portable build)")
        print( "  3. Sign in to your WeChat client")
        print( "  4. In WeChatMsg: pick a chat -> Export -> JSON")
        print( "  5. Note the export directory it writes to")
        print()

    export_dir = _prompt(
        "Where will WeChatMsg write the JSON export?",
        str(Path.home() / "Documents" / "WeChatMsg-export"),
    )

    data = _load_identity(identity_path)
    data.setdefault("wechat", {})["export_dir"] = export_dir
    _save_identity(identity_path, data)

    print()
    print(f"[ok] saved wechat.export_dir = {export_dir} -> {identity_path}")
    print()
    print("Next steps:")
    print("  1. In WeChatMsg, export the chats you want to ingest as JSON")
    print("     (Tools -> Export -> JSON).")
    print(f"  2. Confirm the JSON files landed in: {export_dir}")
    print( "  3. Run:        memexa ingest wechat")
    print( "  4. Query:      memexa quick \"<question>\"")
    print()
    return 0


# -----------------------------------------------------------------------------
# Ingest dispatchers
# -----------------------------------------------------------------------------

def _glue_fetcher_to_l0_input_email(
    raw_root: Path, l0_input_root: Path,
) -> int:
    """Convert per-email fetcher output -> per-batch L0 worker input.

    v0.1.0 had no glue between ``email_history_fetcher`` (writes one
    JSON per email to ``raw_inputs/email/<account>/<date>/<folder>/<uid>.json``)
    and ``l0_worker_api`` (reads ``<batches_dir>/<date>/<batch_id>/prompt.json``).
    The intermediate ``v5_email_batch_builder`` assumes a hand-curated
    src layout that no OSS pipeline ever writes; on a fresh install the
    chain breaks silently with 'discover 0 batches'.

    This helper does the whole transform in one shot: read per-email
    JSONs, regroup per (account, date), call ``_build_prompt`` directly,
    write the L0-ready ``prompt.json`` + ``meta.json`` into the layout
    ``l0_worker_api --batches-dir`` expects. Skips ``v5_email_batch_builder``
    main() entirely.

    Returns the number of L0 batch dirs written (excluding ones already
    present and non-empty).
    """
    import json
    if not raw_root.exists():
        return 0
    l0_input_root.mkdir(parents=True, exist_ok=True)

    from memexa.ingestion.v5_email_batch_builder import (
        _build_prompt, load_manifest, _extract_self_emails,
    )
    manifest_slice = load_manifest()
    self_emails = _extract_self_emails(manifest_slice)

    n_batches = 0
    for account_dir in raw_root.iterdir():
        if not account_dir.is_dir():
            continue
        account = account_dir.name
        for date_dir in account_dir.iterdir():
            if not date_dir.is_dir():
                continue
            date_str = date_dir.name
            emails: list = []
            for folder_dir in date_dir.iterdir():
                if not folder_dir.is_dir():
                    continue
                for uid_json in folder_dir.glob("*.json"):
                    try:
                        emails.append(json.loads(
                            uid_json.read_text(encoding="utf-8")))
                    except Exception:
                        continue
            if not emails:
                continue
            batch_id = f"{account}_{date_str.replace('-', '')}"
            batch_dir = l0_input_root / date_str / batch_id
            prompt_path = batch_dir / "prompt.json"
            if prompt_path.exists():
                # Idempotent: don't overwrite (caller may have already
                # processed this batch; l0_worker_api skips via .done marker)
                continue
            batch_dir.mkdir(parents=True, exist_ok=True)
            try:
                prompt = _build_prompt(
                    batch_id, emails, self_emails, manifest_slice
                )
            except Exception as e:
                print(f"  [warn] _build_prompt failed for {batch_id}: {e}",
                      file=sys.stderr)
                continue
            prompt_path.write_text(
                json.dumps(prompt, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            # meta.json is referenced by some downstream tools; keep it.
            meta = {
                "batch_id": batch_id, "date": date_str, "account": account,
                "n_msgs": prompt.get("n_msgs", len(emails)),
                "source_kind": "email",
            }
            (batch_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            n_batches += 1
    return n_batches


def ingest_email(args: argparse.Namespace) -> int:
    """End-to-end email ingest: fetch IMAP -> build prompts -> extract -> POST.

    Chain (v0.1.1; collapsed to 4 steps after dropping the
    unnecessary v5_email_batch_builder middle step, which assumed a
    src layout no OSS pipeline ever writes):

    1. ``email_history_fetcher``     -- IMAP fetch -> per-email JSON
       at ``data/raw_inputs/email/<account>/<date>/<folder>/<uid>.json``
    2. ``_glue_fetcher_to_l0_input_email`` -- regroup + call
       ``v5_email_batch_builder._build_prompt`` directly -> writes
       ``data/l0_v5/input_batches_email/<date>/<batch_id>/{prompt,meta}.json``
    3. ``l0_worker_api``             -- LLM gate + extract + arbiter
       -> V2 cards at ``data/l0_v5/cards_v2_email/<batch_id>.json``
    4. ``streaming_post_v5``         -- POST cards to Hindsight
       memory_full_v5 bank

    With backend up + LLM env vars set, this single command takes a
    user from "configured account" to "queryable cards."
    """
    from memexa.extraction.email_history_fetcher import _cli as fetcher_cli

    print("[1/4] IMAP fetch...")
    forwarded = ["email_history_fetcher"]
    if getattr(args, "account", None):
        forwarded.extend(["--account", args.account])
    if getattr(args, "since", None):
        forwarded.extend(["--since", args.since])
    if getattr(args, "max_per_folder", None):
        forwarded.extend(["--max-per-folder", str(args.max_per_folder)])
    rc = fetcher_cli(forwarded)
    if rc != 0:
        print(f"[fail] IMAP fetch returned {rc}; aborting pipeline",
              file=sys.stderr)
        return rc

    # fetcher writes to `<install>/data/raw_inputs/email/...` (relative
    # to email_history_fetcher.py::_REPO), not to data_root. Read its
    # _RAW_DIR directly. L0 + post live under data_root for inspectability.
    from memexa.extraction.email_history_fetcher import _RAW_DIR as raw_root
    data_root = _resolve_data_root()
    l0_input = data_root / "l0_v5" / "input_batches_email"
    cards_dir = data_root / "l0_v5" / "cards_v2_email"

    print(f"[2/4] build prompts -> {l0_input}...")
    n = _glue_fetcher_to_l0_input_email(raw_root, l0_input)
    print(f"[ok] {n} new batch prompt(s) written "
          f"(idempotent; already-built batches skipped)")
    if n == 0:
        print("[info] nothing new to extract; pipeline exits 0")
        return 0

    print(f"[3/4] LLM extract -> {cards_dir}...")
    rc = _run_l0_extract(l0_input, cards_dir)
    if rc != 0:
        print(f"[fail] l0_worker_api returned {rc}", file=sys.stderr)
        return rc

    print(f"[4/4] POST cards -> Hindsight backend...")
    rc = _run_streaming_post(cards_dir)
    if rc != 0:
        print(f"[fail] streaming_post_v5 returned {rc}", file=sys.stderr)
        return rc

    print()
    print("[done] end-to-end ingest complete. Try:")
    print("  memexa quick \"<question>\"   -- query the cards you just indexed")
    return 0


# -----------------------------------------------------------------------------
# Backend bootstrap (Docker compose wrapper)
# -----------------------------------------------------------------------------

def _resolve_compose_file() -> Path:
    """Locate the docker-compose.yml memexa will manage.

    Resolution order:
      1. ``~/.memexa/docker-compose.yml`` if it exists (user-customised)
      2. seed from packaged template at ``memexa/templates/docker-compose.yml``
    """
    user_path = _resolve_config_dir() / "docker-compose.yml"
    if user_path.exists():
        return user_path
    # Seed from packaged template
    try:
        from importlib import resources
        pkg = resources.files("memexa.templates")
        tmpl = pkg / "docker-compose.yml"
        if tmpl.is_file():
            user_path.parent.mkdir(parents=True, exist_ok=True)
            user_path.write_bytes(tmpl.read_bytes())
            print(f"[ok] seeded {user_path} from packaged template")
            return user_path
    except Exception as e:
        print(f"[warn] failed to read packaged template: {e}", file=sys.stderr)
    raise FileNotFoundError(
        f"No docker-compose.yml at {user_path}, and packaged template "
        "is unreadable. Re-install memexa or pass --compose-file."
    )


_DOCKER_BIN_CACHE: Optional[str] = None


def _find_docker_bin() -> Optional[str]:
    """Locate the docker binary even when not on PATH.

    Order:
      1. ``MEMEXA_DOCKER_BIN`` env (override)
      2. ``shutil.which("docker")`` (PATH)
      3. Platform-specific common install locations:
         - macOS:   OrbStack xbin, Docker Desktop
         - Windows: Docker Desktop
         - Linux:   /usr/bin, /usr/local/bin (usually on PATH but
                    SSH non-login shells sometimes drop)
    Cached per-process.
    """
    global _DOCKER_BIN_CACHE
    if _DOCKER_BIN_CACHE:
        return _DOCKER_BIN_CACHE
    import shutil
    override = os.environ.get("MEMEXA_DOCKER_BIN", "").strip()
    if override and Path(override).is_file():
        _DOCKER_BIN_CACHE = override
        return override
    found = shutil.which("docker")
    if found:
        _DOCKER_BIN_CACHE = found
        return found
    candidates = []
    if sys.platform == "darwin":
        candidates += [
            "/Applications/OrbStack.app/Contents/MacOS/xbin/docker",
            "/usr/local/bin/docker",
            "/opt/homebrew/bin/docker",
            "/Applications/Docker.app/Contents/Resources/bin/docker",
        ]
    elif sys.platform == "win32":
        candidates += [
            r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
            r"C:\Program Files\Docker\Docker\Resources\bin\docker.exe",
        ]
    else:
        candidates += [
            "/usr/bin/docker", "/usr/local/bin/docker",
        ]
    for c in candidates:
        if Path(c).is_file():
            _DOCKER_BIN_CACHE = c
            return c
    return None


_COMPOSE_STYLE_CACHE: Optional[str] = None  # "plugin" | "standalone"


def _find_compose_style() -> Optional[str]:
    """Detect whether ``docker compose`` (plugin) or standalone
    ``docker-compose`` is available.

    Older Docker daemons (e.g. Ubuntu 22.04's 20.10.x) lack the
    compose plugin but ship the standalone ``docker-compose`` binary.
    """
    global _COMPOSE_STYLE_CACHE
    if _COMPOSE_STYLE_CACHE:
        return _COMPOSE_STYLE_CACHE
    import shutil, subprocess
    docker_bin = _find_docker_bin()
    if docker_bin is not None:
        try:
            rc = subprocess.run(
                [docker_bin, "compose", "version"],
                capture_output=True, timeout=10,
            ).returncode
            if rc == 0:
                _COMPOSE_STYLE_CACHE = "plugin"
                return "plugin"
        except Exception:
            pass
    if shutil.which("docker-compose"):
        _COMPOSE_STYLE_CACHE = "standalone"
        return "standalone"
    return None


def _run_docker(cmd: list, **kwargs) -> int:
    """Run a `docker` subcommand and stream output to stdout/stderr.

    Special handling for ``compose`` subcommand: if the Docker plugin
    is missing but standalone ``docker-compose`` is on PATH, route the
    call to it (transparent fallback for older Docker daemons).
    """
    import subprocess
    # Compose fallback: ["compose", "-f", ...] -> ["docker-compose", "-f", ...]
    if cmd and cmd[0] == "compose":
        style = _find_compose_style()
        if style == "standalone":
            import shutil
            standalone = shutil.which("docker-compose")
            return subprocess.call([standalone] + cmd[1:], **kwargs)
        # plugin path falls through to normal docker call below
    docker_bin = _find_docker_bin()
    if docker_bin is None:
        print("[fail] docker not found. Install Docker Desktop "
              "(Windows/macOS) / OrbStack (macOS) / docker-compose-plugin "
              "(Linux). Or set MEMEXA_DOCKER_BIN=/path/to/docker.",
              file=sys.stderr)
        return 127
    return subprocess.call([docker_bin] + cmd, **kwargs)


def _poll_hindsight(timeout_s: int = 180) -> bool:
    """Poll http://127.0.0.1:8888/healthz until ready or timeout.

    Default 180s because the Hindsight FastAPI container loads BGE-M3
    embedding model on startup (~30-90s on first run); a 60s timeout
    races that. Progress dots so the user knows it's still trying.
    """
    import time
    try:
        import httpx  # type: ignore
    except ImportError:
        print("[warn] httpx not installed; cannot poll backend health",
              file=sys.stderr)
        return False
    deadline = time.time() + timeout_s
    url = os.environ.get("MEMEXA_HINDSIGHT_URL", "http://127.0.0.1:8888")
    last_err = ""
    elapsed = 0
    while time.time() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=3.0)
            if r.status_code < 400:
                if elapsed > 0:
                    print()  # finish progress line
                return True
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = type(e).__name__
        time.sleep(3)
        elapsed += 3
        if elapsed % 15 == 0:
            print(f"  ... still waiting ({elapsed}s, last={last_err})",
                  flush=True)
    print(f"[warn] backend not ready after {timeout_s}s "
          f"(last: {last_err}); check `memexa backend status` and "
          f"`docker logs memexa-hindsight`",
          file=sys.stderr)
    return False


def backend_up(args: argparse.Namespace) -> int:
    """``memexa backend up`` -- bring up pg + Hindsight via docker compose."""
    try:
        compose_file = _resolve_compose_file()
    except FileNotFoundError as e:
        print(f"[fail] {e}", file=sys.stderr)
        return 1
    print(f"[info] using compose file: {compose_file}")
    # Hindsight requires LLM key on startup; warn early if missing.
    env_file = compose_file.parent / ".env"
    has_llm = (
        bool(os.environ.get("MEMEXA_REMOTE_LLM_API_KEY"))
        or bool(os.environ.get("HINDSIGHT_API_LLM_API_KEY"))
        or (env_file.exists() and "MEMEXA_REMOTE_LLM_API_KEY" in
            env_file.read_text(encoding="utf-8"))
    )
    if not has_llm:
        print("[warn] no LLM API key found "
              "(MEMEXA_REMOTE_LLM_API_KEY env var or ~/.memexa/.env). "
              "Hindsight container will fail to start without it. "
              "Run `memexa init llm` first if you have not.",
              file=sys.stderr)
    rc = _run_docker(
        ["compose", "-f", str(compose_file), "up", "-d"],
        cwd=str(compose_file.parent),
    )
    if rc != 0:
        return rc
    print("[info] waiting for Hindsight to report healthy (up to 60s)...")
    if _poll_hindsight(60):
        print("[ok] backend ready at http://127.0.0.1:8888")
        return 0
    print("[warn] backend started but did not become healthy in 60s; "
          "see `docker logs memexa-hindsight` for details",
          file=sys.stderr)
    return 0  # don't fail-hard; user can investigate


def backend_down(args: argparse.Namespace) -> int:
    """``memexa backend down`` -- stop the backend containers."""
    try:
        compose_file = _resolve_compose_file()
    except FileNotFoundError as e:
        print(f"[fail] {e}", file=sys.stderr)
        return 1
    return _run_docker(
        ["compose", "-f", str(compose_file), "down"],
        cwd=str(compose_file.parent),
    )


def backend_status(args: argparse.Namespace) -> int:
    """``memexa backend status`` -- print container state + healthz probe."""
    print("=== docker containers ===")
    _run_docker(["ps", "--filter", "name=memexa-", "--format",
                 "table {{.Names}}\\t{{.Status}}\\t{{.Ports}}"])
    print()
    print("=== Hindsight healthz ===")
    try:
        import httpx  # type: ignore
        url = os.environ.get("MEMEXA_HINDSIGHT_URL", "http://127.0.0.1:8888")
        r = httpx.get(f"{url}/health", timeout=3.0)
        print(f"  {url}/health -> HTTP {r.status_code}")
        if r.status_code < 400:
            print("  [ok] backend reachable")
            return 0
        return 1
    except Exception as e:
        print(f"  [fail] unreachable: {type(e).__name__}: {e}")
        return 1


# -----------------------------------------------------------------------------
# LLM provider wizard
# -----------------------------------------------------------------------------

LLM_PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "gate_model": "deepseek-chat",
        "extract_model": "deepseek-chat",
        "credential_url": "https://platform.deepseek.com/api_keys",
        "note": "Recommended for Chinese workloads; ~¥0.30 / 1K msgs",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "gate_model": "gpt-4o-mini",
        "extract_model": "gpt-4o",
        "credential_url": "https://platform.openai.com/api-keys",
        "note": "GPT-4o; 5-10x DeepSeek cost",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "gate_model": "qwen-turbo",
        "extract_model": "qwen-plus",
        "credential_url": "https://dashscope.console.aliyun.com",
        "note": "Alibaba Qwen via DashScope; competitive Chinese pricing",
    },
    "custom": {
        "base_url": "",
        "gate_model": "",
        "extract_model": "",
        "credential_url": "(provider-specific)",
        "note": "OpenAI-compatible endpoint (USTC LiteLLM, vLLM, Ollama, etc.)",
    },
}


def init_llm_wizard(args: argparse.Namespace) -> int:
    """``memexa init llm`` -- pick a provider and write env vars to .env."""
    cfg_dir = _resolve_config_dir()
    env_path = cfg_dir / ".env"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    print()
    print("memexa init llm -- LLM provider setup wizard")
    print("-" * 50)
    print(f"Config file: {env_path}")
    print()
    print("Available providers:")
    for name, meta in LLM_PROVIDERS.items():
        print(f"  - {name:<10s}  {meta['note']}")
    print()

    while True:
        choice = _prompt("Pick provider", "deepseek").lower()
        if choice in LLM_PROVIDERS:
            break
        print(f"  unknown: {choice!r}; valid: {sorted(LLM_PROVIDERS)}")

    meta = LLM_PROVIDERS[choice]
    base_url = _prompt("Base URL", meta["base_url"] or None)
    gate_model = _prompt("Gate-LLM model name", meta["gate_model"] or None)
    extract_model = _prompt("Extractor model name", meta["extract_model"] or None)

    print()
    print(f"Get your API key from: {meta['credential_url']}")
    api_key = _prompt(
        "API key (paste it now; written to .env with chmod 600)",
        default=None,
    )

    lines = []
    if env_path.exists():
        # preserve other env vars; only rewrite MEMEXA_REMOTE_LLM_*
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip().startswith("MEMEXA_REMOTE_LLM_"):
                lines.append(raw)
    lines.extend([
        f"MEMEXA_REMOTE_LLM_BASE_URL={base_url}",
        f"MEMEXA_REMOTE_LLM_API_KEY={api_key}",
        f"MEMEXA_REMOTE_LLM_GATE_MODEL={gate_model}",
        f"MEMEXA_REMOTE_LLM_EXTRACT_MODEL={extract_model}",
    ])
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except Exception:
        pass  # Windows ACL won't honor chmod, fine

    print()
    print(f"[ok] wrote LLM provider config -> {env_path}")
    print()
    print("Next steps:")
    print(f"  1. (optional) Inspect:  cat {env_path}")
    print( "  2. Source the env:     set -a; . ~/.memexa/.env; set +a")
    print( "                         (PowerShell:  Get-Content ~/.memexa/.env | ForEach-Object { if ($_ -match '^([^=]+)=(.*)$') { Set-Item -Path env:$($matches[1]) -Value $matches[2] } })")
    print( "  3. memexa backend up   -- bring up pg + Hindsight")
    print( "  4. memexa init email   -- onboard an email account")
    print( "  5. memexa ingest email -- fetch + extract + index")
    print( "  6. memexa quick \"<question>\"")
    return 0


# -----------------------------------------------------------------------------
# Full end-to-end ingest pipeline (builder -> extract -> POST)
# -----------------------------------------------------------------------------

def _resolve_data_root() -> Path:
    """Resolve memexa's data root (where batches + cards live)."""
    try:
        from memexa.core._path_resolver import data_dir
        return data_dir()
    except Exception:
        return Path(os.environ.get("MEMEXA_DATA_DIR",
                                    str(_resolve_config_dir() / "data"))).expanduser()


def _run_l0_extract(batches_dir: Path, cards_dir: Path) -> int:
    """Drive ``l0_worker_api`` to extract V2 cards from batches.

    Wraps the module's own argparse main(). The LLM client picks up
    MEMEXA_REMOTE_LLM_BASE_URL / API_KEY / GATE_MODEL / EXTRACT_MODEL
    from the environment, so the caller is responsible for sourcing
    `~/.memexa/.env` (or whatever the user wrote with `memexa init llm`)
    before calling `memexa ingest`.
    """
    from memexa.extraction import l0_worker_api
    cards_dir.mkdir(parents=True, exist_ok=True)
    done_dir = cards_dir.parent / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    # l0_worker_api.main(argv) bypasses sys.argv -- pass argv directly
    return l0_worker_api.main([
        "--batches-dir", str(batches_dir),
        "--out-dir", str(cards_dir),
        "--done-dir", str(done_dir),
        "--max-batches", "0",  # 0 = unlimited; one-shot, no daemon mode
    ])


def _run_streaming_post(cards_dir: Path) -> int:
    """Drive ``streaming_post_v5`` to POST cards into Hindsight."""
    from memexa.extraction import streaming_post_v5
    posted_dir = cards_dir.parent / "posted"
    posted_dir.mkdir(parents=True, exist_ok=True)
    old_argv = sys.argv
    sys.argv = [
        "streaming_post_v5",
        "--cards-dir", str(cards_dir),
        "--posted-marker-dir", str(posted_dir),
        "--exit-when-empty-rounds", "3",  # don't run forever
        "--max-iterations", "100",
    ]
    try:
        return streaming_post_v5.main()
    except SystemExit as e:
        return int(e.code or 0)
    finally:
        sys.argv = old_argv


def _build_wechat_prompt_from_messages(
    batch_id: str, chat_room: str, messages: list,
) -> Dict[str, Any]:
    """Build a v5 prompt dict for one chat-day batch from a flat
    message list.

    Schema accepted (demo format, also produced by trivial adapters
    from WeChatMsg JSON):
        [{room, sender, send_time, content}, ...]
        OR with field aliases:
        [{chat_room, sender_name|sender|NickName, ts|send_time|CreateTime, content|StrContent}, ...]

    v0.1.1: writes a minimal V5 envelope sufficient for l0_worker_api
    to extract cards. Full WeChat features (group meta, reply chains,
    image stickers) deferred to v0.3 official adapter.
    """
    import hashlib
    msgs = []
    senders = set()
    timestamps = []
    for m in messages:
        sender = (m.get("sender") or m.get("sender_name") or
                  m.get("NickName") or m.get("Remark") or "unknown")
        ts_str = (m.get("send_time") or m.get("ts") or m.get("CreateTime")
                  or "")
        content = (m.get("content") or m.get("StrContent")
                   or m.get("DisplayContent") or "")
        if not content:
            continue
        if isinstance(ts_str, (int, float)):
            from datetime import datetime, timezone
            ts_iso = datetime.fromtimestamp(
                float(ts_str), tz=timezone.utc
            ).isoformat()
        else:
            ts_iso = str(ts_str)
        try:
            from datetime import datetime
            ts_epoch = datetime.fromisoformat(ts_iso.replace("Z", "+00:00")
                                              ).timestamp()
            timestamps.append(ts_epoch)
        except Exception:
            pass
        wxid_hash = hashlib.sha1(sender.encode("utf-8")).hexdigest()[:12]
        msgs.append({
            "ts": ts_iso,
            "wxid_hash": wxid_hash,
            "sender": sender,
            "content": str(content),
        })
        senders.add(sender)

    if timestamps:
        from datetime import datetime, timezone
        win_start = datetime.fromtimestamp(min(timestamps),
                                           tz=timezone.utc).isoformat()
        win_end = datetime.fromtimestamp(max(timestamps),
                                         tz=timezone.utc).isoformat()
    else:
        win_start = win_end = ""

    room_hash = hashlib.sha1(chat_room.encode("utf-8")).hexdigest()[:12]
    return {
        "batch_id": batch_id,
        "chat_room": chat_room,
        "room_hash": room_hash,
        "batch_window_local": [win_start, win_end],
        "sender_list": [{"sender": s,
                          "wxid_hash":
                          hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]}
                         for s in sorted(senders)],
        "manifest_slice": {"persons": {}},
        "messages": msgs,
        "schema_v_input": "v5",
        "v5_native_builder": "wizards._build_wechat_prompt_from_messages",
        "source_kind": "wechat",
        "n_msgs": len(msgs),
        "n_unique_senders": len(senders),
        "is_group_chat": len(senders) > 2,
    }


def _glue_wechat_json_dir_to_l0_input(
    export_dir: Path, l0_input_root: Path,
) -> int:
    """Read a directory of per-chat JSON files (demo / WeChatMsg-adapted
    schema), regroup per (chat_room, date), and write l0_worker_api-
    ready prompt.json + meta.json.

    Layouts accepted:
      <dir>/<chat-name>/messages.json    (WeChatMsg per-chat dir)
      <dir>/*.json                       (flat per-chat files)
      <dir>/messages.json                (single chat)
    """
    import json
    if not export_dir.exists():
        return 0
    l0_input_root.mkdir(parents=True, exist_ok=True)
    n = 0
    # Discover candidate JSON files
    candidates = []
    if (export_dir / "messages.json").exists():
        candidates.append(export_dir / "messages.json")
    for child in export_dir.iterdir():
        if child.is_file() and child.suffix.lower() == ".json":
            candidates.append(child)
        elif child.is_dir():
            mj = child / "messages.json"
            if mj.exists():
                candidates.append(mj)

    for jpath in candidates:
        try:
            data = json.loads(jpath.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                continue
        except Exception:
            continue
        # Group per (chat_room, date)
        from collections import defaultdict
        groups = defaultdict(list)
        for m in data:
            room = (m.get("room") or m.get("chat_room") or jpath.stem)
            ts_str = str(m.get("send_time") or m.get("ts")
                         or m.get("CreateTime") or "")[:10]
            if not ts_str:
                continue
            groups[(room, ts_str)].append(m)
        for (room, date_str), msgs in groups.items():
            import hashlib
            batch_id = hashlib.sha1(
                f"{room}|{date_str}".encode("utf-8")
            ).hexdigest()[:16]
            batch_dir = l0_input_root / date_str / batch_id
            prompt_path = batch_dir / "prompt.json"
            if prompt_path.exists():
                continue
            batch_dir.mkdir(parents=True, exist_ok=True)
            prompt = _build_wechat_prompt_from_messages(batch_id, room, msgs)
            prompt_path.write_text(
                json.dumps(prompt, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            (batch_dir / "meta.json").write_text(
                json.dumps({"batch_id": batch_id, "date": date_str,
                            "chat_room": room, "source_kind": "wechat"},
                            ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            n += 1
    return n


def ingest_wechat(args: argparse.Namespace) -> int:
    """End-to-end WeChat ingest from a JSON export directory.

    Chain:
      1. Read per-chat JSON files (demo schema, or WeChatMsg-adapted)
         from --from <dir> or wechat.export_dir in identity.yaml.
      2. _glue_wechat_json_dir_to_l0_input regroups per (chat, date)
         and writes prompt.json + meta.json under
         data/l0_v5/input_batches_wechat/<date>/<batch_id>/
      3. l0_worker_api -- LLM extract V2 cards
      4. streaming_post_v5 -- POST to Hindsight

    Note (v0.1.1 honesty): the schema accepted is the demo-dataset
    flat-message format `[{room, sender, send_time, content}]`.
    WeChatMsg's native export uses similar but richer fields
    (CreateTime, StrContent, NickName, etc.); both are accepted via
    field-name aliasing in _build_wechat_prompt_from_messages.
    Full WeChatMsg feature parity (replies, sub_msg_types, image
    stickers) is on the v0.3 roadmap.
    """
    cfg_dir = _resolve_config_dir()
    identity_path = cfg_dir / "identity.yaml"

    export_dir = getattr(args, "from_", None) or getattr(args, "from_dir", None)
    if not export_dir:
        data = _load_identity(identity_path)
        export_dir = (data.get("wechat") or {}).get("export_dir")
    if not export_dir:
        print("[fail] no WeChat export directory specified.", file=sys.stderr)
        print( "       Either pass --from <dir>, or run `memexa init wechat`",
               file=sys.stderr)
        return 1
    export_path = Path(export_dir).expanduser()
    if not export_path.exists():
        print(f"[fail] export directory not found: {export_path}",
              file=sys.stderr)
        return 1

    data_root = _resolve_data_root()
    l0_input = data_root / "l0_v5" / "input_batches_wechat"
    cards_dir = data_root / "l0_v5" / "cards_v2_wechat"

    print(f"[1/3] reading WeChat JSON exports from {export_path}...")
    n = _glue_wechat_json_dir_to_l0_input(export_path, l0_input)
    print(f"[ok] {n} new batch prompt(s) written")
    if n == 0:
        print("[info] nothing new to extract; pipeline exits 0")
        return 0

    print(f"[2/3] LLM extract -> {cards_dir}...")
    rc = _run_l0_extract(l0_input, cards_dir)
    if rc != 0:
        print(f"[fail] l0_worker_api returned {rc}", file=sys.stderr)
        return rc

    print(f"[3/3] POST cards -> Hindsight backend...")
    rc = _run_streaming_post(cards_dir)
    if rc != 0:
        print(f"[fail] streaming_post_v5 returned {rc}", file=sys.stderr)
        return rc

    print()
    print("[done] WeChat end-to-end ingest complete. Try:")
    print("  memexa quick \"<question>\"")
    return 0
