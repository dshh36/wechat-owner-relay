"""Non-secret, installation-specific settings; no account data ships here."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


def settings():
    path = ROOT / "project_config.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def owner_contact(config):
    # The legacy carry_* switches remain compatible; the nickname is configurable.
    return config.get("owner_contact", "owner")


def candidate_command(ids):
    python = str(sys.executable).replace("'", "''")
    script = str(ROOT / "watcher.py").replace("'", "''")
    return f"& '{python}' '{script}' --events " + " ".join(ids)
