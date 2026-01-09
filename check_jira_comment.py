#!/usr/bin/env python3
"""Check the latest JIRA comment on DEV-2504."""
import requests
import os
from dotenv import load_dotenv

load_dotenv()

jira_url = os.environ["JIRA_URL"]
jira_user = os.environ["JIRA_USER"]
jira_token = os.environ["JIRA_TOKEN"]

# Get issue with comments
resp = requests.get(
    f"{jira_url}/rest/api/3/issue/DEV-2504",
    auth=(jira_user, jira_token),
    headers={"Accept": "application/json"},
    params={"fields": "comment"},
    timeout=10
)

if resp.status_code == 200:
    data = resp.json()
    comments = data['fields']['comment']['comments']
    if comments:
        latest = comments[-1]
        print(f"Latest comment from: {latest['author']['displayName']}")
        print(f"Posted: {latest['created']}")
        print(f"\n{latest['body']}")
    else:
        print("No comments found")
else:
    print(f"Error: {resp.status_code}")
    print(resp.text[:500])
