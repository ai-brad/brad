#!/usr/bin/env python3
"""Local smoke test for Brad's restart-summary flow.

This script is intentionally side-effect free with respect to Jira and PRs:
it uses the dummy ticketing adapter, runs only the requirements-analysis path,
and monkeypatches the implementation phase so the workflow stops before any
branch push or PR creation.

The smoke still exercises real credentials for the Codex-backed summarization
path when BRAD_HARNESS=codex and CODEX_SUMMARIZATION_MODEL (or CODEX_MODEL)
are configured.
"""
from __future__ import annotations

import argparse
import copy
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

from brad import db
from brad.adf_parser import adf_to_text
from brad.config import load_config, validate_config
from brad.orchestrator import BradOrchestrator


def _default_fixture_path() -> Path:
    return Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "dummy_ticket.txt"


def _build_smoke_config(args):
    cfg = load_config()
    workdir = Path(
        args.workdir
        or tempfile.mkdtemp(prefix="brad-summary-smoke-")
    ).resolve()
    repo_path = Path(args.repo_path or (workdir / "repo")).resolve()
    db_path = Path(args.db_path or (workdir / "brad_data.db")).resolve()
    attachments_dir = Path(args.attachments_dir or (workdir / "attachments")).resolve()
    dummy_ticket_path = Path(args.dummy_ticket_path or _default_fixture_path()).resolve()
    dummy_ticket_log_path = Path(args.dummy_ticket_log_path or (workdir / "dummy_ticket.log")).resolve()

    workdir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    smoke_cfg = replace(
        cfg,
        ticketing_adapter="dummy",
        target_repo_path=str(repo_path),
        db_path=str(db_path),
        attachments_dir=str(attachments_dir),
        dummy_ticket_path=str(dummy_ticket_path),
        dummy_ticket_log_path=str(dummy_ticket_log_path),
    )
    return smoke_cfg, workdir


def _require_codex_harness(cfg) -> None:
    if (cfg.harness or "").strip().lower() != "codex":
        raise SystemExit("This smoke test requires BRAD_HARNESS=codex.")
    if not shutil.which(cfg.codex_bin or "codex"):
        raise SystemExit(f"Codex CLI not found on PATH: {cfg.codex_bin or 'codex'}")


def _run_pass(orchestrator: BradOrchestrator, issue: dict, pass_no: int) -> None:
    issue_key = issue["key"]
    orchestrator.logger.info("Running smoke pass %s for %s", pass_no, issue_key)
    orchestrator._process_issue(copy.deepcopy(issue))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workdir",
        help="Directory for the temp clone, DB, and logs. Defaults to a fresh temp dir.",
    )
    parser.add_argument(
        "--repo-path",
        help="Explicit target repo clone path. Defaults to <workdir>/repo.",
    )
    parser.add_argument(
        "--db-path",
        help="Explicit SQLite DB path. Defaults to <workdir>/brad_data.db.",
    )
    parser.add_argument(
        "--attachments-dir",
        help="Explicit attachments dir. Defaults to <workdir>/attachments.",
    )
    parser.add_argument(
        "--dummy-ticket-path",
        help="Override the dummy ticket fixture path.",
    )
    parser.add_argument(
        "--dummy-ticket-log-path",
        help="Override the dummy ticket adapter log path.",
    )
    parser.add_argument(
        "--issue-key",
        help="Dummy issue key to process. Defaults to the first BradReview issue.",
    )
    args = parser.parse_args()

    load_dotenv(override=True)
    cfg, workdir = _build_smoke_config(args)
    validate_config(cfg)
    _require_codex_harness(cfg)
    db.init_db(cfg.db_path)

    orchestrator = BradOrchestrator(cfg)
    orchestrator._handle_implementation_phase = lambda *_, **__: orchestrator.logger.info(
        "Smoke mode: skipping implementation / PR creation."
    )

    issues = orchestrator.ticketing.fetch_issues_with_label("BradReview")
    if not issues:
        raise SystemExit("Dummy ticket adapter returned no BradReview issues.")

    issue = None
    if args.issue_key:
        for candidate in issues:
            if candidate.get("key") == args.issue_key:
                issue = candidate
                break
        if issue is None:
            issue = orchestrator.ticketing.fetch_issue(args.issue_key)
            if issue is None:
                raise SystemExit(f"Could not find dummy issue {args.issue_key!r}.")
    else:
        issue = issues[0]

    issue_key = issue["key"]
    description = adf_to_text(issue.get("fields", {}).get("description", ""))
    repo_path = str(orchestrator.repo.repo_path)
    execution_id = db.create_execution(
        issue_key,
        issue.get("fields", {}).get("summary", ""),
        issue_title=(issue.get("fields", {}).get("summary", "") or "").strip(),
        action="Summary smoke",
        model_name=orchestrator._summarization_model_name(),
    )

    print(f"workdir={workdir}")
    print(f"repo_path={repo_path}")
    print(f"db_path={cfg.db_path}")
    print(f"dummy_ticket_log={cfg.dummy_ticket_log_path}")

    # First pass: build and persist a continuation summary, then run a real
    # requirements-analysis turn that receives that summary in the prompt.
    seed_text = "\n\n".join(
        part for part in (
            f"{issue_key}: {issue.get('fields', {}).get('summary', '').strip()}",
            description.strip(),
        ) if part
    ).strip()
    if not seed_text:
        raise SystemExit("Dummy issue did not provide any text to summarize.")

    summary = orchestrator._compact_issue_context(
        issue_key,
        seed_text,
        source="summary_smoke_seed",
        execution_id=execution_id,
    )
    if not summary:
        raise SystemExit("Codex summarization returned an empty summary.")

    stored = db.get_issue_context_summary(repo_path, issue_key)
    stored_summary = (stored or {}).get("summary", "")
    if stored_summary != summary:
        raise SystemExit("Persisted summary did not match the generated summary.")

    execution = db.get_execution(execution_id) or {}
    print("initial_summary=" + summary[:500].replace("\n", "\\n"))
    print(
        "summary_tokens="
        + str(execution.get("continuation_summary_total_tokens") or 0)
        + " summary_cost=$"
        + f"{float(execution.get('continuation_summary_cost') or 0.0):.4f}"
    )

    first_response = orchestrator.agent.invoke_requirements_analysis(
        issue_key=issue_key,
        description=description,
        attachment_paths=[],
        repo_path=repo_path,
        iteration=0,
        continuation_context=summary,
    )
    refreshed = orchestrator._refresh_issue_continuation_summary(
        issue_key,
        first_response,
        "summary_smoke_requirements",
        execution_id=execution_id,
    )
    if not refreshed:
        raise SystemExit("Failed to refresh the persisted summary after a real model turn.")

    print("first_response_action=" + str(first_response.get("action", "")))
    print("refreshed_summary=" + refreshed[:500].replace("\n", "\\n"))

    # Second pass: re-run the issue so the orchestrator loads the stored
    # summary and feeds it back into the next prompt.
    orchestrator.ticketing.add_label(issue_key, "BradReview")
    second_issue = orchestrator.ticketing.fetch_issue(issue_key) or copy.deepcopy(issue)
    _run_pass(orchestrator, second_issue, 2)

    db.finish_execution(execution_id, status="completed")

    loaded_again = db.get_issue_context_summary(repo_path, issue_key)
    if not loaded_again or not (loaded_again.get("summary") or "").strip():
        raise SystemExit("Summary was not still present after the second pass.")

    print("smoke_status=ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
