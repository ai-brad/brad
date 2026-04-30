#!/usr/bin/env python3
"""One-off script to process pending review comments."""
from dotenv import load_dotenv
import os
load_dotenv(override=os.environ.get("BRAD_DOTENV_OVERRIDE", "true").lower() != "false")

from brad.orchestrator import BradOrchestrator
from brad.config import load_config

cfg = load_config()
orch = BradOrchestrator(cfg)

print("Processing pending review comments...")
orch._process_review_comments()
print("Done.")
