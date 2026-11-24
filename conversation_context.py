"""Local, explicitly verified unanswered messages; never reads or sends WeChat."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from clawbot_relay import CONFIG, binding
from watcher import ROOT, selected_events, load, now, save

STATE = ROOT / "wechat_conversation_context.json"


def update(event_id, contact, incoming=None, answered=None):
    event = selected_events([event_id])[0]
    if not contact or event.get("contact_candidate") != contact:
        raise ValueError("Context must match the selected candidate's exact contact")
    _, _, _, _, identity = binding(load(CONFIG, {}))
    state = load(STATE, {"binding_fingerprint": identity, "contacts": {}})
    if state.get("binding_fingerprint") != identity:
        raise ValueError("Context account binding changed")
    records = state.setdefault("contacts", {}).setdefault(contact, {})
    incoming = [] if incoming is None else incoming
    answered = [] if answered is None else answered
    if not isinstance(incoming, list) or len(incoming) > 50:
        raise ValueError("Expected at most 50 verified incoming messages")
    additions = {}
    for item in incoming:
        if not isinstance(item, dict) or set(item) != {"anchor", "text"}:
            raise ValueError("Expected verified text and a stable visible time/position anchor")
        anchor, text = item["anchor"], item["text"]
        if (not isinstance(anchor, str) or not 1 <= len(anchor) <= 200
                or not isinstance(text, str) or not 1 <= len(text) <= 4000):
            raise ValueError("Invalid verified message")
        key = hashlib.sha256(json.dumps(
            [identity, contact, anchor, text], ensure_ascii=False).encode()).hexdigest()
        if key not in records:
            additions[key] = {"anchor": anchor, "text": text,
                              "state": "pending", "verified_at": now()}
    if (not isinstance(answered, list) or any(key not in records for key in answered)
            or len(set(answered)) != len(answered)):
        raise ValueError("Only previously verified messages can be resolved")
    if len(records) + len(additions) > 500:
        raise ValueError("Context limit reached; review locally before adding messages")
    records.update(additions)
    for key in answered:
        records[key].update(state="answered", answered_at=now())
    if incoming or answered:
        save(STATE, state)
    return {"contact": contact, "added": len(additions), "pending": [
        {"id": key, **record} for key, record in records.items()
        if record["state"] == "pending"
    ]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--contact", required=True)
    parser.add_argument("--incoming-file", type=Path)
    parser.add_argument("--answered", nargs="+")
    args = parser.parse_args()
    incoming = (json.loads(args.incoming_file.read_text(encoding="utf-8-sig"))
                if args.incoming_file else None)
    print(json.dumps(update(args.event, args.contact, incoming, args.answered), ensure_ascii=False))
