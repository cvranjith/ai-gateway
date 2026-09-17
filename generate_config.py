#!/usr/bin/env python3
"""
CLI wrapper around auth.create_client() — registers a new OAuth client
in auth_config.json (gitignored) for each name passed on the command
line, printing its generated client_id + client_secret once.

Note: the gateway also does this itself automatically (see
auth.ensure_bootstrap_client()) the very first time it starts with no
clients registered at all, and the /ui dashboard can register further
clients once you're signed in — this script is only needed for
registering additional clients from the command line instead.

Usage:
    python3 generate_config.py ytrun_ios
    python3 generate_config.py ytrun_ios second_brain_app
"""

import sys

from auth import create_client


def main():
    names = sys.argv[1:]
    if not names:
        print("Usage: python3 generate_config.py <client_name> [<client_name> ...]")
        sys.exit(1)

    for name in names:
        client_id, client_secret = create_client(name)
        print(f"{name}:")
        print(f"  client_id:     {client_id}")
        print(f"  client_secret: {client_secret}")
        print()

    print("Saved to auth_config.json (mode 600). Save the credentials above "
          "somewhere safe now — client_secret is not shown again after this.")


if __name__ == "__main__":
    main()
