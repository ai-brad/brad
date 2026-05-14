#!/usr/bin/env python3
"""
Brad - Autonomous AI Software Engineer
Main entry point for the Brad orchestrator.
"""

import os
import sys
import time
import argparse
from dotenv import load_dotenv
from brad.config import load_config, validate_config
from brad.execution_liveness import ExecutionLivenessTracker
from brad.logging_config import setup_logging, get_logger
from brad.orchestrator import BradOrchestrator
from brad import db


STALE_EXECUTION_MESSAGE = "Worker restarted during execution"


def main():
    liveness = None
    parser = argparse.ArgumentParser(
        description="Brad - Autonomous AI Software Engineer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python brad.py run --once          # Process issues once
  python brad.py run --loop          # Process continuously (sleeps LOOP_INTERVAL seconds)
  python brad.py --log-level DEBUG   # Enable debug logging
        """
    )

    parser.add_argument(
        "command",
        choices=["run"],
        help="Command to execute"
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit"
    )

    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously, sleeping LOOP_INTERVAL seconds between iterations (default 300)"
    )

    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Override log level from config"
    )

    args = parser.parse_args()

    # Load .env file.  By default override=True so values in .env always win
    # over a stale shell session (e.g. AZURE_OPENAI_API_KEY exported in
    # ~/.bashrc that has since been rotated).  Set BRAD_DOTENV_OVERRIDE=false
    # to let the shell environment take precedence instead.
    _dotenv_override = os.environ.get("BRAD_DOTENV_OVERRIDE", "true").lower() != "false"
    load_dotenv(override=_dotenv_override)

    # Load configuration
    try:
        cfg = load_config()

        # Override log level if specified
        if args.log_level:
            cfg.log_level = args.log_level

        # Setup logging
        log_file = setup_logging(log_level=cfg.log_level)
        logger = get_logger(__name__)

        logger.info("Brad starting up")
        logger.info(f"Log file: {log_file}")
        logger.info(f"Target repository: {cfg.target_repo_path}")
        logger.info(f"JIRA URL: {cfg.jira_url}")
        logger.info(f"GitHub repo: {cfg.github_repo}")

        # Validate configuration
        validate_config(cfg)
        logger.info("Configuration validated")

        # Initialize database
        db.init_db(cfg.db_path)
        logger.info(f"Database initialized at {cfg.db_path}")
        liveness = ExecutionLivenessTracker.from_env()
        db.set_execution_runtime_observer(liveness)
        reconciled = db.reconcile_running_executions(
            STALE_EXECUTION_MESSAGE,
            stale_after_seconds=int(os.environ.get("EXECUTION_STALE_THRESHOLD_SECONDS", "300")),
            current_worker_id=liveness.worker_id,
            assume_single_worker=os.environ.get("BRAD_ALLOW_CONCURRENT_WORKERS", "false").lower() == "false",
        )
        if reconciled:
            logger.warning(
                "Marked %d stale running execution(s) as failed during startup",
                reconciled,
            )
        liveness.install_signal_handlers()

    except Exception as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    # Initialize orchestrator
    try:
        orchestrator = BradOrchestrator(cfg)
    except Exception as e:
        logger = get_logger(__name__)
        logger.error(f"Failed to initialize Brad: {e}", exc_info=True)
        if liveness is not None:
            liveness.close()
        sys.exit(1)

    # Execute command
    try:
        if args.command == "run":
            if args.loop:
                loop_interval = int(os.environ.get("LOOP_INTERVAL", "300"))
                logger.info(f"Starting loop mode (interval: {loop_interval}s)")
                while True:
                    try:
                        orchestrator.run_once()
                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        logger.error(f"run_once failed: {e}", exc_info=True)
                    logger.info(f"Loop: sleeping {loop_interval}s")
                    time.sleep(loop_interval)
            else:
                orchestrator.run_once()

        logger.info("Brad completed successfully")
        if liveness is not None:
            liveness.close()

    except KeyboardInterrupt:
        logger.info("Brad interrupted by user")
        if liveness is not None:
            try:
                liveness.close()
            except Exception:
                pass
        sys.exit(0)
    except Exception as e:
        logger.error(f"Brad failed with error: {e}", exc_info=True)
        if liveness is not None:
            try:
                liveness.close()
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
