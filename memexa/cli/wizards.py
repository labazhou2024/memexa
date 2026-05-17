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

def _glue_fetcher_to_builder_email(
    raw_root: Path, builder_src: Path,
) -> int:
    """Convert per-email fetcher output -> per-batch builder input.

    v0.1.0 had no glue between ``email_history_fetcher`` (writes one
    JSON per email to ``data/raw_inputs/email/<account>/<date>/<folder>/<uid>.json``)
    and ``v5_email_batch_builder`` (reads a ``raw.json`` list of emails
    per batch directory at ``<src>/<date>/<batch_id>/raw.json``). This
    helper bridges the two layouts so end-to-end ingest works.

    Returns the number of batches written.
    """
    import json
    if not raw_root.exists():
        return 0
    builder_src.mkdir(parents=True, exist_ok=True)
    n_batches = 0
    # Group by <account>/<date>; each (account, date) becomes one batch.
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
            batch_dir = builder_src / date_str / batch_id
            batch_dir.mkdir(parents=True, exist_ok=True)
            raw_out = batch_dir / "raw.json"
            raw_out.write_text(
                json.dumps(emails, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            n_batches += 1
    return n_batches


def ingest_email(args: argparse.Namespace) -> int:
    """End-to-end email ingest: fetch IMAP -> build batches -> extract -> POST.

    Chain (v0.1.1; replaces the v0.1.0 broken pipeline):

    1. ``email_history_fetcher`` -- IMAP fetch raw emails to
       ``data/raw_inputs/email/<account>/<date>/<folder>/<uid>.json``
    2. ``_glue_fetcher_to_builder_email`` -- regroup per-(account, date)
       into ``data/raw_email_batches/<date>/<batch_id>/raw.json``
    3. ``v5_email_batch_builder`` -- transform raw.json -> prompt.json at
       ``data/l0_v5/input_batches_email/<date>/<batch_id>/prompt.json``
    4. ``l0_worker_api`` -- LLM gate + extract + arbiter -> V2 cards at
       ``data/l0_v5/cards_v2_email/<batch_id>.json``
    5. ``streaming_post_v5`` -- POST cards to Hindsight memory_full_v5

    With backend up + LLM env vars set, this single command takes a
    user from "configured account" to "queryable cards."
    """
    from memexa.extraction.email_history_fetcher import _cli as fetcher_cli
    from memexa.ingestion import v5_email_batch_builder

    print("[1/5] IMAP fetch...")
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

    data_root = _resolve_data_root()
    raw_root = data_root / "raw_inputs" / "email"
    builder_src = data_root / "raw_email_batches"
    builder_out = data_root / "l0_v5" / "input_batches_email"
    cards_dir = data_root / "l0_v5" / "cards_v2_email"

    print(f"[2/5] grouping per-(account,date) batches under {builder_src}...")
    n = _glue_fetcher_to_builder_email(raw_root, builder_src)
    print(f"[ok] {n} batch(es) regrouped")
    if n == 0:
        print("[info] nothing new to extract; pipeline exits 0")
        return 0

    print(f"[3/5] builder transform -> {builder_out}...")
    old_argv = sys.argv
    sys.argv = [
        "v5_email_batch_builder",
        "--src", str(builder_src),
        "--out", str(builder_out),
        "--skip-existing",
    ]
    try:
        rc = v5_email_batch_builder.main()
    except SystemExit as e:
        rc = int(e.code or 0)
    finally:
        sys.argv = old_argv
    if rc != 0:
        print(f"[fail] builder returned {rc}", file=sys.stderr)
        return rc

    print(f"[4/5] LLM extract -> {cards_dir}...")
    rc = _run_l0_extract(builder_out, cards_dir)
    if rc != 0:
        print(f"[fail] l0_worker_api returned {rc}", file=sys.stderr)
        return rc

    print(f"[5/5] POST cards -> Hindsight backend...")
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


def _run_docker(cmd: list, **kwargs) -> int:
    """Run a `docker` subcommand and stream output to stdout/stderr."""
    import subprocess
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
            r = httpx.get(f"{url}/healthz", timeout=3.0)
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
        r = httpx.get(f"{url}/healthz", timeout=3.0)
        print(f"  {url}/healthz -> HTTP {r.status_code}")
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


def ingest_wechat(args: argparse.Namespace) -> int:
    """Ingest a WeChatMsg export directory.

    Resolves ``--from`` flag first, then ``wechat.export_dir`` in
    identity.yaml, then errors with a hint.
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
    print(f"[info] reading WeChat export from {export_path}")
    # Delegate to the existing builder. Re-uses the v5 pipeline so any
    # downstream consumer (extractor, dashboard) stays uniform.
    from memexa.ingestion import v5_wechat_batch_builder
    try:
        # The builder uses its own arg parser; we wire stdin args via sys.argv
        # so user-overrides flow through cleanly.
        old_argv = sys.argv
        sys.argv = [
            "v5_wechat_batch_builder",
            "--src", str(export_path),
        ]
        try:
            return v5_wechat_batch_builder.main()
        finally:
            sys.argv = old_argv
    except SystemExit as e:
        return int(e.code or 0)
