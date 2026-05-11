"""Local briefing builder — runs on the laptop via Task Scheduler at 07:25.

Pipeline:
1. Read curated slices of the Personal OS at c:\\vsCode\\.me.
2. Build a sanitized JSON snapshot.
3. Encrypt with AES-GCM (key from Key Vault via Azure CLI auth).
4. Upload to Blob `briefing-context/today.bin`. Overwrite.

Logs (Hard Rule 1): only sizes and timings. Never content.

Idempotent: re-running just overwrites today's blob.

Run manually:
    .\\.venv\\Scripts\\python.exe scripts\\local\\briefing_builder.py
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.storage.blob import BlobServiceClient
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"

# Personal OS layout. Adjust if the OS path moves.
ME_ROOT = Path(os.environ.get("ME_OS_ROOT", r"c:\vsCode\.me"))
DASHBOARD = ME_ROOT / "_dashboard.md"
JOURNAL_DIR_FMT = "05_journal/{year}/{year}-{month:02d}-{day:02d}.md"
AREAS_DIR = ME_ROOT / "02_areas"

log = logging.getLogger("mindMe.briefing-builder")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("azure").setLevel(logging.WARNING)


def _today_journal_path(today: date) -> Path:
    return ME_ROOT / JOURNAL_DIR_FMT.format(
        year=today.year, month=today.month, day=today.day
    )


def _extract_dashboard_sections(text: str) -> dict:
    """Parse `_dashboard.md` into a few well-known slices.

    Heuristic — looks for level-2 headers like `## Top goals` etc. Robust to
    minor edits.
    """
    sections: dict[str, list[str]] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    for line in text.splitlines():
        header = re.match(r"^##\s+(.+?)\s*$", line)
        if header:
            if current_key is not None:
                sections[current_key] = current_lines
            current_key = header.group(1).strip().lower()
            current_lines = []
        else:
            if current_key is not None:
                current_lines.append(line)

    if current_key is not None:
        sections[current_key] = current_lines

    def first_bullets(key_substrings: list[str], limit: int = 5) -> list[str]:
        for k, lines in sections.items():
            if any(s in k for s in key_substrings):
                bullets = [
                    re.sub(r"^[-*]\s+", "", ln).strip()
                    for ln in lines
                    if re.match(r"^\s*[-*]\s+", ln)
                ]
                return [b for b in bullets if b][:limit]
        return []

    return {
        "top_goals": first_bullets(["top goal", "goal"]),
        "this_week": first_bullets(["this week", "week"]),
        "today_focus": " ".join(first_bullets(["today", "focus"])) or "",
    }


def _extract_journal_summary(path: Path) -> dict:
    if not path.exists():
        return {"date": None, "open_loops_count": 0, "mood": "", "energy": ""}

    text = path.read_text(encoding="utf-8", errors="replace")
    open_loops = len(re.findall(r"^\s*-\s*\[\s*\]", text, flags=re.MULTILINE))
    mood = ""
    energy = ""
    mood_match = re.search(r"Mood\s*:\s*([0-9]{1,2})", text)
    energy_match = re.search(r"Energy\s*:\s*([0-9]{1,2})", text)
    if mood_match:
        mood = mood_match.group(1)
    if energy_match:
        energy = energy_match.group(1)
    return {
        "date": path.stem,
        "open_loops_count": open_loops,
        "mood": mood,
        "energy": energy,
    }


def _build_snapshot() -> dict:
    today = date.today()
    snapshot: dict = {"date": today.isoformat()}

    if DASHBOARD.exists():
        snapshot.update(
            _extract_dashboard_sections(
                DASHBOARD.read_text(encoding="utf-8", errors="replace")
            )
        )
    else:
        log.warning("dashboard not found at %s — using empty values", DASHBOARD)
        snapshot.update({"top_goals": [], "this_week": [], "today_focus": ""})

    journal_path = _today_journal_path(today)
    snapshot["yesterday"] = _extract_journal_summary(journal_path)

    # Area headlines: just the H1 of each area README.
    headlines: list[str] = []
    if AREAS_DIR.exists():
        for readme in sorted(AREAS_DIR.glob("*/README.md")):
            try:
                first_line = readme.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()[0]
                if first_line.startswith("# "):
                    headlines.append(first_line[2:].strip())
            except Exception:
                continue
    snapshot["areas"] = headlines[:8]

    return snapshot


def _encrypt(snapshot: dict, key: bytes) -> bytes:
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    plaintext = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)
    return nonce + ciphertext


def _resolve_encryption_key(credential: DefaultAzureCredential) -> bytes:
    """Fetch the 32-byte AES-GCM key from Key Vault and decode."""
    vault_name = os.environ["AZURE_KEYVAULT_NAME"]
    secret_name = os.environ.get("AZURE_KEYVAULT_ENCRYPTION_KEY_SECRET", "briefing-encryption-key")
    sc = SecretClient(
        vault_url=f"https://{vault_name}.vault.azure.net",
        credential=credential,
    )
    secret = sc.get_secret(secret_name)
    key = base64.b64decode(secret.value)
    if len(key) != 32:
        raise ValueError(
            f"Key Vault secret '{secret_name}' must be 32 bytes base64-encoded; got {len(key)}."
        )
    return key


def _upload(blob_bytes: bytes, credential: DefaultAzureCredential) -> int:
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    container = os.environ.get("AZURE_STORAGE_BRIEFING_CONTAINER", "briefing-context")
    bsc = BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=credential,
    )
    blob = bsc.get_blob_client(container=container, blob="today.bin")
    blob.upload_blob(blob_bytes, overwrite=True)
    return len(blob_bytes)


def main() -> int:
    _setup_logging()
    load_dotenv(ENV_PATH)

    required = ["AZURE_KEYVAULT_NAME", "AZURE_STORAGE_ACCOUNT"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        log.error("missing required .env vars: %s", ", ".join(missing))
        return 2

    started = time.monotonic()
    snapshot = _build_snapshot()
    plaintext_size = len(json.dumps(snapshot, ensure_ascii=False))

    credential = DefaultAzureCredential()
    key = _resolve_encryption_key(credential)
    blob_bytes = _encrypt(snapshot, key)
    uploaded = _upload(blob_bytes, credential)

    duration = time.monotonic() - started
    log.info(
        "briefing uploaded plaintext=%d ciphertext=%d duration=%.2fs",
        plaintext_size, uploaded, duration,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
