#!/usr/bin/env python3
"""Check all recent JIRA comments on DEV-2504."""
import requests
import os
from dotenv import load_dotenv

load_dotenv()

jira_url = os.environ["JIRA_URL"]
jira_user = os.environ["JIRA_USER"]
jira_token = os.environ["JIRA_TOKEN"]

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
    print(f"Total comments: {len(comments)}\n")
    
    # Show last 3 comments
    for comment in comments[-3:]:
        author = comment['author']['displayName']
        created = comment['created']
        body = comment['body']
        
        # Extract text from ADF
        if isinstance(body, dict) and 'content' in body:
            text_parts = []
            for item in body['content']:
                if item.get('type') == 'paragraph' and 'content' in item:
                    for content in item['content']:
                        if content.get('type') == 'text':
                            text_parts.append(content.get('text', ''))
            text = '\n'.join(text_parts)
        else:
            text = str(body)
        
        print(f"{'='*80}")
        print(f"From: {author}")
        print(f"Time: {created}")
        print(f"\n{text[:1000]}")
        print()
else:
    print(f"Error: {resp.status_code}")
