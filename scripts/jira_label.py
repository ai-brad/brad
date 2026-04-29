#!/usr/bin/env python3
"""Add or remove a label on a Jira issue using credentials from .env.

Usage:
    python scripts/jira_label.py <ISSUE-KEY> <LABEL> [--remove]

Examples:
    python scripts/jira_label.py DEV-3205 BradReview
    python scripts/jira_label.py DEV-3205 BradReview --remove

The script loads JIRA_URL, JIRA_USER (or JIRA_EMAIL), and JIRA_TOKEN
(or JIRA_API_TOKEN) from the working-directory .env via python-dotenv.
"""
from __future__ import annotations

import argparse
import os
import sys

import requests
from dotenv import load_dotenv


def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("issue_key", help="Jira issue key, e.g. DEV-3205")
    parser.add_argument("label", help="Label to add (or remove with --remove)")
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove the label instead of adding it",
    )
    args = parser.parse_args()

    # override=True so values in .env always win over a stale shell session.
    # Otherwise `JIRA_TOKEN` exported from a previous login can silently mask
    # the token in .env and you end up authed as the wrong account.
    load_dotenv(override=True)

    url = _env("JIRA_URL")
    email = _env("JIRA_USER", "JIRA_EMAIL")
    token = _env("JIRA_TOKEN", "JIRA_API_TOKEN")
    if not (url and email and token):
        print(
            "ERROR: missing one of JIRA_URL / JIRA_USER (or JIRA_EMAIL) / "
            "JIRA_TOKEN (or JIRA_API_TOKEN) in environment.",
            file=sys.stderr,
        )
        return 2

    base = url.rstrip("/")
    op = "remove" if args.remove else "add"
    payload = {"update": {"labels": [{op: args.label}]}}

    resp = requests.put(
        f"{base}/rest/api/3/issue/{args.issue_key}",
        auth=(email, token),
        json=payload,
        timeout=30,
    )
    if resp.status_code not in (200, 204):
        print(
            f"ERROR: PUT {resp.status_code} {resp.text[:500]}",
            file=sys.stderr,
        )
        return 1

    # Confirm by reading current labels back.
    get = requests.get(
        f"{base}/rest/api/3/issue/{args.issue_key}?fields=labels",
        auth=(email, token),
        timeout=30,
    )
    labels = get.json().get("fields", {}).get("labels", []) if get.ok else []
    print(
        f"{'Removed' if args.remove else 'Added'} label "
        f"{args.label!r} on {args.issue_key}. Current labels: {labels}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
