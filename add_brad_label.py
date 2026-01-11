#!/usr/bin/env python3
"""Add BradReview label back to the issue."""
import sys
from dotenv import load_dotenv
from config import load_config
from jira_client import JiraClient

if len(sys.argv) < 2:
    print("Usage: python add_brad_label.py <ISSUE_KEY>")
    sys.exit(1)

load_dotenv()
cfg = load_config()
jira = JiraClient(cfg)

issue_key = sys.argv[1]
print(f"Adding BradReview label to {issue_key}...")
jira.add_label(issue_key, "BradReview")
print(f"✓ Label added to {issue_key}")
