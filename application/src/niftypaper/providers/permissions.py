"""Local record of which providers' terms permit paper trading with their data (config/local/provider_permissions.json, git-ignored).

A provider is permitted only if the file says virtual_trading_permitted is exactly true, gives a non-empty evidence note (for example
"FYERS support e-mail of 2026-11-02") and a confirmed_on date that is not in the future. Anything else, including a missing or unreadable file,
means NOT permitted. The program cannot check the evidence; it records that you stated it. Example (copy to the path above and edit):
{"schema_version": 1, "providers": {"fyers": {"virtual_trading_permitted": true, "evidence": "<where the written permission is kept>", "confirmed_on": "2026-11-02"}}}
"""
import json
from datetime import date
from pathlib import Path

DEFAULT_PATH = Path("config") / "local" / "provider_permissions.json"


def load_permissions(path, today=None):
    """{provider id: (permitted, note)}. Never raises; problems are reported in the note and mean not permitted."""
    path = Path(path)
    today = today or date.today()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, "no permissions file found"
    except (OSError, ValueError):
        return {}, "permissions file is unreadable or not valid JSON"
    entries = data.get("providers") if isinstance(data, dict) and data.get("schema_version") == 1 else None
    if not isinstance(entries, dict):
        return {}, "permissions file has an unexpected format"
    result = {}
    for provider_id, entry in entries.items():
        result[provider_id] = _check(entry, today)
    return result, "permissions file read"


def _check(entry, today):
    if not isinstance(entry, dict) or entry.get("virtual_trading_permitted") is not True:
        return False, "not recorded as permitted"
    evidence, confirmed = entry.get("evidence"), entry.get("confirmed_on")
    if not isinstance(evidence, str) or len(evidence.strip()) < 10:
        return False, "permission recorded without an evidence note"
    try:
        when = date.fromisoformat(confirmed)
    except (TypeError, ValueError):
        return False, "permission recorded without a valid confirmed_on date"
    if when > today:
        return False, "confirmed_on date is in the future"
    return True, f"you recorded permission on {when.isoformat()} (evidence: {evidence.strip()[:80]})"
