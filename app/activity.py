"""Persistent app activity: what reviewers did to which claims, and when claims were first flagged.

Stored as local files (JSON lines and JSON) so reports can read them across sessions.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

STATE_DIR = Path(os.environ.get("E4H_STATE_DIR", Path(__file__).resolve().parents[1] / "app_data"))
LOG_FILE = STATE_DIR / "activity_log.jsonl"
FLAGGED_FILE = STATE_DIR / "first_flagged.json"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def append(entries: list[dict]) -> None:
    """Add entries to the activity log. Each gets a timestamp if it has none."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as stream:
        for entry in entries:
            stream.write(json.dumps({"timestamp": now(), **entry}) + "\n")


def read_log() -> list[dict]:
    """Return every logged entry, oldest first."""
    if not LOG_FILE.exists():
        return []
    with LOG_FILE.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def record_flagged(workspace: str, flagged: dict[str, dict]) -> None:
    """Remember when each claim was first seen needing review.

    Input: workspace name and {claim_id: {"agency", "codes": all issue codes, "auto": one-click codes}}.
    """
    seen = read_flagged()
    new = {
        f"{workspace}:{claim_id}": {"first_flagged": now(), **info}
        for claim_id, info in flagged.items() if f"{workspace}:{claim_id}" not in seen
    }
    if new:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        FLAGGED_FILE.write_text(json.dumps({**seen, **new}, indent=1), encoding="utf-8")


def read_flagged() -> dict:
    """Return {"workspace:claim_id": {"first_flagged": iso time, "codes": [...]}}."""
    if not FLAGGED_FILE.exists():
        return {}
    return json.loads(FLAGGED_FILE.read_text(encoding="utf-8"))


def clear() -> None:
    """Delete all saved activity (used by the app's reset)."""
    for path in (LOG_FILE, FLAGGED_FILE, STATE_DIR / "resubmissions.jsonl"):
        if path.exists():
            path.unlink()
