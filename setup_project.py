"""Create disabled local configuration without copying login or message data."""
import argparse
import json
from pathlib import Path
import sys
from uuid import UUID

ROOT = Path(__file__).resolve().parent


def initialize(root, thread_id, owner_contact="owner", account_label="my-secondary-account"):
    UUID(thread_id)
    if not owner_contact.strip() or not account_label.strip():
        raise ValueError("Account label and owner contact must be nonempty")
    configs = {
        "clawbot_relay_config.json": {"enabled": False, "thread_id": thread_id},
        "wechat_notifier_config.json": {
            "enabled": False, "thread_id": thread_id, "owner_contact": owner_contact,
            "carry_replies": False, "idle_seconds": 2, "wait_seconds": 1200,
            "max_batch_size": 8,
        },
        "project_config.json": {"account_label": account_label},
        "bridge_config.json": {"enabled": False},
        "desktop_bridge_config.json": {"enabled": False, "thread_id": thread_id},
    }
    paths = [root / name for name in configs]
    if any(path.exists() for path in paths):
        raise FileExistsError("Existing configuration preserved; edit it explicitly")
    for name, value in configs.items():
        # Exclusive creation also refuses a concurrently created configuration.
        with (root / name).open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
    return list(configs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread-id", required=True, help="Existing bound local Codex chat UUID")
    parser.add_argument("--owner-contact", default="owner", help="Exact verified owner nickname")
    parser.add_argument("--account-label", default="my-secondary-account")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Live detection and relay require Windows")
    created = initialize(ROOT, args.thread_id, args.owner_contact, args.account_label)
    print(f"Created {len(created)} disabled configurations. No services started.")


if __name__ == "__main__":
    main()
