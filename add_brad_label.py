#!/usr/bin/env python3
"""Add BradReview label back to the issue."""
from dotenv import load_dotenv
from config import load_config
from jira_client import JiraClient

load_dotenv()
cfg = load_config()
jira = JiraClient(cfg)

issue_key = "DEV-2504"
print(f"Adding BradReview label to {issue_key}...")
jira.add_label(issue_key, "BradReview")
print(f"✓ Label added to {issue_key}")
