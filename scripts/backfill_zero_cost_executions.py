#!/usr/bin/env python3
"""Backfill zero-cost Brad executions from stored token counts and model pricing.

The script only touches finished executions whose `total_cost` is still 0.0.
It is dry-run by default; pass `--apply` to persist the recomputed totals.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from brad import db


def _resolve_db_path(cli_value: str | None) -> str:
    if cli_value:
        return str(Path(cli_value).expanduser().resolve())
    env_value = os.environ.get("BRAD_DB_PATH")
    if env_value:
        return str(Path(env_value).expanduser().resolve())
    return str(Path("brad_data.db").resolve())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db-path",
        help="SQLite database path. Defaults to BRAD_DB_PATH or ./brad_data.db.",
    )
    parser.add_argument(
        "--issue-key",
        help="Only backfill executions for a single Jira issue key.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Only inspect the first N zero-cost executions.",
    )
    parser.add_argument(
        "--fallback-model",
        default="gpt-5.4",
        help="Model to assume when an execution is missing model_name.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the recalculated costs instead of only printing a dry run.",
    )
    args = parser.parse_args()

    load_dotenv(override=True)

    db_path = _resolve_db_path(args.db_path)
    db.init_db(db_path)

    candidates = db.get_zero_cost_executions(limit=args.limit, issue_key=args.issue_key)
    if not candidates:
        print("No finished zero-cost executions were found.")
        return 0

    estimated_total = 0.0
    for execution in candidates:
        cost = db.estimate_execution_cost(execution, fallback_model_name=args.fallback_model)
        estimated_total += cost
        print(
            f"execution #{execution['id']} {execution.get('issue_key', '')} "
            f"model={execution.get('model_name') or args.fallback_model} "
            f"prompt={int(execution.get('total_prompt_tokens') or 0)} "
            f"cached={int(execution.get('total_cached_prompt_tokens') or 0)} "
            f"completion={int(execution.get('total_completion_tokens') or 0)} "
            f"cost=${cost:.6f}"
        )

    print(f"candidates={len(candidates)} estimated_total_cost=${estimated_total:.6f}")

    if not args.apply:
        print("dry_run=ok")
        return 0

    updates = db.backfill_zero_cost_executions(
        limit=args.limit,
        issue_key=args.issue_key,
        fallback_model_name=args.fallback_model,
    )
    updated_total = sum(float(row["cost"]) for row in updates)
    print(f"updated={len(updates)} backfilled_total_cost=${updated_total:.6f}")
    print("apply=ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
