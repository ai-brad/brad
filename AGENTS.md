`brad` owns the worker, GUI, orchestrator, adapters, prompts, and DB migrations; if a behavior change must reach `brad-vm`, push `main` here and redeploy from `/home/seb/deploy-brad`.
Before pushing run `pytest -q`; when debugging a live failure, use the UI/API first, then confirm with VM worker logs and `/home/sebastian/brad/brad_data.db` instead of guessing from summaries.
If a Python dependency looks missing, verify the active uv venv and rerun the matching `uv sync` first.
