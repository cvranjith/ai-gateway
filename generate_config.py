#!/usr/bin/env python3
"""
Generates auth_config.json (gitignored) with a fresh jwt_secret and,
for each client name passed on the command line, a random client_id +
client_secret pair. Run this once per new client you want to register;
re-running for a name that already exists leaves its credentials
unchanged and only adds genuinely new names.

Usage:
    python3 generate_config.py ytrun_ios
    python3 generate_config.py ytrun_ios second_brain_app
"""

import json
import secrets
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "auth_config.json"


def main():
    names = sys.argv[1:]
    if not names:
        print("Usage: python3 generate_config.py <client_name> [<client_name> ...]")
        sys.exit(1)

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    else:
        config = {"jwt_secret": secrets.token_urlsafe(48), "clients": {}}

    for name in names:
        client_id = f"{name}_{secrets.token_hex(4)}"
        client_secret = secrets.token_urlsafe(32)
        config["clients"][client_id] = client_secret
        print(f"{name}:")
        print(f"  client_id:     {client_id}")
        print(f"  client_secret: {client_secret}")
        print()

    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    CONFIG_PATH.chmod(0o600)

    print(f"Wrote {CONFIG_PATH} (mode 600). Save the credentials above somewhere "
          "safe now — client_secret is not shown again after this.")


if __name__ == "__main__":
    main()
