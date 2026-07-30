"""[DEPRECATED 2026-05-16] Local briefing builder.

Replaced by the cloud-native flow:
- `scripts/local/sync_os_to_blob.py` syncs OS markdown to `personal-os/` container.
- `harness/function_app.py::_build_briefing_snapshot()` builds the briefing
  in-process when the Foundry agent calls `get_briefing_context()`.

Kept here for reference / fallback while the new path stabilizes. Do not
schedule via Task Scheduler. See docs/architecture.md for the new design.

Original docstring follows.
---

Local briefing builder — was scheduled on the laptop via Task Scheduler at 07:25.

Pipeline:
1. Read curated slices of the Personal OS at %USERPROFILE%\\OneDrive\\.vscode\\.me (override via ME_OS_ROOT env var).
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
import hashlib
import json
import logging
import os
import sys
import re
import time
import zlib
from datetime import date, datetime, timezone
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.storage.blob import BlobServiceClient
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"

# One definition of the folder-role map, shared with the deployed function.
sys.path.insert(0, str(REPO_ROOT / "harness"))
import vault_layout  # noqa: E402

# Personal OS layout. Lives in OneDrive for cross-device sync.
# Override via ME_OS_ROOT env var if the OS path moves.
ME_ROOT = Path(
    os.environ.get(
        "ME_OS_ROOT",
        os.path.expandvars(r"%USERPROFILE%\OneDrive\.vscode\.me"),
    )
)
DASHBOARD = ME_ROOT / "home.md"
JOURNAL_DIR_FMT = (
    vault_layout.folder(vault_layout.PERSONAL_OS, "journal")
    + "/{year}/{year}-{month:02d}-{day:02d}.md"
)
AREAS_DIR = ME_ROOT / vault_layout.folder(vault_layout.PERSONAL_OS, "areas")

SCHEMA_VERSION = "2.0.0"
CORE_MAX_BYTES = int(os.environ.get("BRIEFING_CORE_MAX_BYTES", "3500"))
EXTENDED_MAX_BYTES = int(os.environ.get("BRIEFING_EXTENDED_MAX_BYTES", "7000"))
DEEP_MAX_BYTES = int(os.environ.get("BRIEFING_DEEP_MAX_BYTES", "9000"))
MAX_EXTENDED_ITEMS = int(os.environ.get("BRIEFING_MAX_EXTENDED_ITEMS", "20"))
MAX_DEEP_ITEMS = int(os.environ.get("BRIEFING_MAX_DEEP_ITEMS", "8"))
DEEP_ZLIB_LEVEL = int(os.environ.get("BRIEFING_DEEP_ZLIB_LEVEL", "9"))

DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
URGENCY_HINTS = ("urgent", "today", "asap", "deadline", "due", "blocker")
CONFIDENCE_BASELINE = 0.35
CONFIDENCE_SCALE = 12.0
CONFIDENCE_CAP = 0.99

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


def _sanitize_text(text: str, max_len: int = 240) -> str:
    cleaned = " ".join(text.split()).strip()
    if len(cleaned) <= max_len:
        return cleaned
    return f"{cleaned[: max_len - 1].rstrip()}…"


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _safe_rel(path: Path) -> str:
    try:
        return str(path.relative_to(ME_ROOT))
    except ValueError:
        return str(path)


def _extract_due_date(text: str) -> date | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        log.debug("ignoring invalid due date in text")
        return None


def _score_text(
    text: str,
    *,
    today: date,
    source_date: date | None = None,
    unresolved: bool = False,
    active_project: bool = False,
    last_touched_days: int | None = None,
) -> tuple[float, str]:
    score = 1.0
    low = text.lower()

    if any(h in low for h in URGENCY_HINTS):
        score += 3.0
    if unresolved:
        score += 2.5
    if active_project:
        score += 1.5
    if source_date == today:
        score += 1.5

    due = _extract_due_date(text)
    urgency = "normal"
    if due is not None:
        delta = (due - today).days
        if delta <= 1:
            score += 4.0
            urgency = "high"
        elif delta <= 3:
            score += 2.5
            urgency = "high"
        elif delta <= 7:
            score += 1.0
            urgency = "normal"
    elif any(h in low for h in ("urgent", "asap", "today", "blocker")):
        urgency = "high"

    if last_touched_days is not None:
        if last_touched_days <= 1:
            score += 2.0
        elif last_touched_days <= 7:
            score += 1.0

    return score, urgency


def _candidate(
    *,
    title: str,
    summary: str,
    source_type: str,
    source_ref: str,
    tags: list[str],
    today: date,
    source_date: date | None = None,
    unresolved: bool = False,
    active_project: bool = False,
    long_excerpt: str = "",
    last_touched_days: int | None = None,
) -> dict:
    score, urgency = _score_text(
        f"{title} {summary}",
        today=today,
        source_date=source_date,
        unresolved=unresolved,
        active_project=active_project,
        last_touched_days=last_touched_days,
    )
    digest = hashlib.sha256(f"{source_type}|{source_ref}|{title}".encode("utf-8")).hexdigest()
    return {
        "id": digest[:16],
        "title": _sanitize_text(title, 140),
        "summary": _sanitize_text(summary, 260),
        "source_type": source_type,
        "source_ref": source_ref,
        "tags": [_sanitize_text(t, 32) for t in tags if t][:4],
        "date": source_date.isoformat() if source_date else None,
        "urgency": urgency,
        "relevance_score": round(score, 2),
        # Confidence is a bounded heuristic for retrieval ranking, not a model probability.
        "confidence": round(
            min(CONFIDENCE_CAP, CONFIDENCE_BASELINE + score / CONFIDENCE_SCALE), 2
        ),
        "long_excerpt": _sanitize_text(long_excerpt, 1400) if long_excerpt else "",
    }


def _trim_entries(entries: list[dict], budget_bytes: int) -> list[dict]:
    trimmed = list(entries)
    while trimmed and _json_size({"entries": trimmed}) > budget_bytes:
        trimmed.pop()
    return trimmed


def _fit_core_budget(core: dict, budget_bytes: int) -> dict:
    reduced = dict(core)
    for field in ("urgent_deadlines", "this_week", "top_goals", "areas"):
        reduced.setdefault(field, [])

    while _json_size(reduced) > budget_bytes:
        if reduced["urgent_deadlines"]:
            reduced["urgent_deadlines"].pop()
            continue
        if reduced["this_week"]:
            reduced["this_week"].pop()
            continue
        if reduced["top_goals"]:
            reduced["top_goals"].pop()
            continue
        if reduced["areas"]:
            reduced["areas"].pop()
            continue
        if len(reduced.get("today_focus", "")) > 80:
            reduced["today_focus"] = _sanitize_text(reduced["today_focus"], 80)
            continue
        break
    return reduced


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


def _extract_journal_highlights(path: Path, today: date) -> list[dict]:
    if not path.exists():
        return []

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    candidates: list[dict] = []
    for ln in lines:
        if len(candidates) >= 6:
            break
        line = ln.strip()
        if not line:
            continue
        unresolved = bool(re.match(r"^-\s*\[\s*\]", line))
        checked = bool(re.match(r"^-\s*\[[xX]\]", line))
        if unresolved or checked:
            clean = re.sub(r"^-\s*\[[xX\s]\]\s*", "", line).strip()
            if not clean:
                continue
            candidates.append(
                _candidate(
                    title=clean,
                    summary="Journal action item",
                    source_type="journal",
                    source_ref=_safe_rel(path),
                    tags=["journal", "open-loop" if unresolved else "done"],
                    today=today,
                    source_date=today,
                    unresolved=unresolved,
                    long_excerpt=clean,
                )
            )
        elif line.startswith("## "):
            heading = line[3:].strip()
            if heading:
                candidates.append(
                    _candidate(
                        title=heading,
                        summary="Journal heading",
                        source_type="journal",
                        source_ref=_safe_rel(path),
                        tags=["journal", "heading"],
                        today=today,
                        source_date=today,
                        long_excerpt=heading,
                    )
                )
    return candidates


def _extract_dashboard_candidates(text: str, today: date) -> list[dict]:
    sections: list[tuple[str, str]] = []
    current_header = ""
    for ln in text.splitlines():
        header = re.match(r"^##\s+(.+?)\s*$", ln)
        if header:
            current_header = header.group(1).strip()
            continue
        bullet = re.match(r"^\s*[-*]\s+(.+?)\s*$", ln)
        if bullet and current_header:
            sections.append((current_header, bullet.group(1).strip()))

    out: list[dict] = []
    for header, bullet in sections[:30]:
        active_project = "goal" in header.lower() or "week" in header.lower()
        out.append(
            _candidate(
                title=bullet,
                summary=f"Dashboard / {header}",
                source_type="dashboard",
                source_ref="_dashboard.md",
                tags=["dashboard", header.lower()[:24]],
                today=today,
                source_date=today,
                unresolved=bullet.strip().startswith("[ ]"),
                active_project=active_project,
                long_excerpt=bullet,
            )
        )
    return out


def _extract_area_candidates(today: date) -> tuple[list[str], list[dict]]:
    headlines: list[str] = []
    candidates: list[dict] = []
    if not AREAS_DIR.exists():
        return headlines, candidates

    for readme in sorted(AREAS_DIR.glob("*/README.md")):
        try:
            text = readme.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            if not lines:
                continue
            h1 = lines[0]
            if h1.startswith("# "):
                headlines.append(_sanitize_text(h1[2:].strip(), 80))

            bullets = [
                re.sub(r"^\s*[-*]\s+", "", ln).strip()
                for ln in lines[1:]
                if re.match(r"^\s*[-*]\s+", ln)
            ]
            summary_parts = [_sanitize_text(b, 90) for b in bullets[:3] if b]
            summary = "; ".join(summary_parts) or _sanitize_text(
                next(
                    (ln.strip() for ln in lines[1:] if ln.strip() and not ln.startswith("#")),
                    "",
                ),
                180,
            )

            touched_days = max(0, int((time.time() - readme.stat().st_mtime) // 86400))
            area_name = readme.parent.name
            candidates.append(
                _candidate(
                    title=h1[2:].strip() if h1.startswith("# ") else area_name,
                    summary=summary or "Area status snapshot",
                    source_type="area",
                    source_ref=_safe_rel(readme),
                    tags=["area", area_name],
                    today=today,
                    last_touched_days=touched_days,
                    long_excerpt="\n".join(lines[:20]),
                )
            )
        except Exception:
            continue
    return headlines[:8], candidates


def _build_snapshot() -> dict:
    today = date.today()
    generated_at = datetime.now(timezone.utc).isoformat()

    dashboard_text = ""
    if DASHBOARD.exists():
        dashboard_text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
        dashboard_sections = _extract_dashboard_sections(dashboard_text)
    else:
        log.warning("dashboard not found at %s — using empty values", DASHBOARD)
        dashboard_sections = {"top_goals": [], "this_week": [], "today_focus": ""}

    journal_path = _today_journal_path(today)
    journal_summary = _extract_journal_summary(journal_path)
    area_headlines, area_candidates = _extract_area_candidates(today)

    candidates: list[dict] = []
    if dashboard_text:
        candidates.extend(_extract_dashboard_candidates(dashboard_text, today))
    candidates.extend(_extract_journal_highlights(journal_path, today))
    candidates.extend(area_candidates)
    candidates.sort(key=lambda item: item.get("relevance_score", 0), reverse=True)

    urgent_deadlines = [
        c["title"]
        for c in candidates
        if c.get("urgency") == "high" or _extract_due_date(c.get("title", "") or "")
    ][:5]

    core = {
        "date": today.isoformat(),
        "top_goals": dashboard_sections.get("top_goals", [])[:6],
        "this_week": dashboard_sections.get("this_week", [])[:6],
        "today_focus": dashboard_sections.get("today_focus", ""),
        "yesterday": journal_summary,
        "areas": area_headlines[:8],
        "urgent_deadlines": urgent_deadlines,
    }
    core = _fit_core_budget(core, CORE_MAX_BYTES)

    extended_entries = [
        {
            "id": c["id"],
            "title": c["title"],
            "summary": c["summary"],
            "source_type": c["source_type"],
            "source_ref": c["source_ref"],
            "tags": c["tags"],
            "date": c["date"],
            "urgency": c["urgency"],
            "relevance_score": c["relevance_score"],
            "confidence": c["confidence"],
        }
        for c in candidates[:MAX_EXTENDED_ITEMS]
    ]
    extended_entries = _trim_entries(extended_entries, EXTENDED_MAX_BYTES)

    deep_entries: list[dict] = []
    for c in candidates[:MAX_DEEP_ITEMS]:
        excerpt = c.get("long_excerpt") or c.get("summary") or ""
        if not excerpt:
            continue
        compressed = zlib.compress(excerpt.encode("utf-8"), level=max(1, min(9, DEEP_ZLIB_LEVEL)))
        deep_entries.append(
            {
                "id": c["id"],
                "title": c["title"],
                "source_type": c["source_type"],
                "source_ref": c["source_ref"],
                "tags": c["tags"],
                "date": c["date"],
                "urgency": c["urgency"],
                "relevance_score": c["relevance_score"],
                "confidence": c["confidence"],
                "content_encoding": "zlib+base64",
                "content_b64": base64.b64encode(compressed).decode("ascii"),
                "raw_len": len(excerpt),
            }
        )
    deep_entries = _trim_entries(deep_entries, DEEP_MAX_BYTES)

    meta = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "candidate_count": len(candidates),
        "budgets_bytes": {
            "core": CORE_MAX_BYTES,
            "extended": EXTENDED_MAX_BYTES,
            "deep": DEEP_MAX_BYTES,
        },
        "payload_bytes": {
            "core": _json_size(core),
            "extended": _json_size({"entries": extended_entries}),
            "deep": _json_size({"entries": deep_entries}),
        },
        "compatibility": {
            "legacy_flat_core_supported": True,
            "legacy_schema": "1.x",
        },
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "date": today.isoformat(),
        "generated_at": generated_at,
        "tiers": {
            "core": core,
            "extended": {"entries": extended_entries},
            "deep": {"entries": deep_entries},
        },
        "meta": meta,
    }


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
