"""Git repository operations manager."""
import os
import subprocess
from pathlib import Path
from typing import Optional
from brad.logging_config import get_logger


class RepoManager:
    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.repo_path = Path(cfg.target_repo_path)
        self.github_repo = getattr(cfg, "github_repo", "")
        self.github_token = getattr(cfg, "github_token", "")
        self.logger.info(f"Initialized repo manager for {self.repo_path}")

        self._bootstrap_clone()

        if not (self.repo_path / ".git").exists():
            raise ValueError(f"Not a git repository: {self.repo_path}")

        self._ensure_tokenized_origin()
        self._ensure_git_identity()

    # -------------------------
    # Bootstrap helpers
    # -------------------------
    def _tokenized_origin_url(self) -> Optional[str]:
        if not self.github_repo or not self.github_token:
            return None
        return f"https://x-access-token:{self.github_token}@github.com/{self.github_repo}.git"

    def _bootstrap_clone(self) -> None:
        """If the target path is missing, clone github_repo into it via HTTPS+token."""
        if self.repo_path.exists() and (self.repo_path / ".git").exists():
            return

        url = self._tokenized_origin_url()
        if url is None:
            # No way to self-bootstrap; leave the existing error paths to complain.
            return

        if self.repo_path.exists() and any(self.repo_path.iterdir()):
            raise ValueError(
                f"Cannot clone into non-empty non-git directory: {self.repo_path}"
            )

        self.repo_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger.info(f"Cloning {self.github_repo} into {self.repo_path}")
        result = subprocess.run(
            ["git", "clone", url, str(self.repo_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            # Redact the token from any error output
            safe_err = (result.stderr or "").replace(self.github_token, "<redacted>")
            raise RuntimeError(
                f"Failed to clone {self.github_repo} into {self.repo_path}: {safe_err.strip()}"
            )

    def _ensure_tokenized_origin(self) -> None:
        """Keep origin pointed at the tokenized HTTPS URL if we have a token.
        Best-effort: silently skips if git isn't happy (e.g. minimal fixture repos).
        """
        url = self._tokenized_origin_url()
        if url is None:
            return
        probe = self._run_git("remote", "get-url", "origin", check=False)
        if probe.returncode != 0:
            return
        if probe.stdout.strip() != url:
            self._run_git("remote", "set-url", "origin", url, check=False)

    def _ensure_git_identity(self) -> None:
        """Set a sensible default user.email / user.name if none is configured.
        Best-effort: silently skips if git isn't happy.
        """
        email_probe = self._run_git("config", "user.email", check=False)
        if email_probe.returncode == 0 and not email_probe.stdout.strip():
            self._run_git("config", "user.email", "brad@users.noreply.github.com", check=False)
        name_probe = self._run_git("config", "user.name", check=False)
        if name_probe.returncode == 0 and not name_probe.stdout.strip():
            self._run_git("config", "user.name", "Brad", check=False)

    # -------------------------
    # Shell helpers
    # -------------------------
    def _run_git(self, *args, check=True):
        cmd = ["git", "-C", str(self.repo_path)] + list(args)
        self.logger.debug(f"Running git command: {' '.join(args)}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
        )

        if check and result.returncode != 0:
            self.logger.error(f"Git command failed: {' '.join(args)}")
            self.logger.error(f"stdout: {result.stdout}")
            self.logger.error(f"stderr: {result.stderr}")
            raise RuntimeError(
                f"Git command failed: {' '.join(args)}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )

        if result.stdout.strip():
            self.logger.debug(f"Git output: {result.stdout.strip()}")

        return result

    # -------------------------
    # Branching
    # -------------------------

    def prepare_branch(self, branch_name: str, base_branch: str = "main"):
        """Prepare a new feature branch from specified base branch."""
        if base_branch != "main":
            self.logger.warning(f"Non-main base branch '{base_branch}' requested - forcing 'main'")
            base_branch = "main"

        self.logger.info(f"Preparing branch '{branch_name}' from '{base_branch}'")

        self.logger.info(f"Checking out {base_branch}")
        self._run_git("checkout", "-f", base_branch)
        self._run_git("fetch", "origin")
        self.logger.info(f"Resetting to origin/{base_branch}")
        self._run_git("reset", "--hard", f"origin/{base_branch}")

        # Delete local branch if exists
        existing_branches = self._run_git("branch", check=False).stdout
        if branch_name in existing_branches:
            self.logger.info(f"Deleting existing local branch '{branch_name}'")
            try:
                self._run_git("branch", "-D", branch_name)
            except RuntimeError as e:
                if "not found" in str(e):
                    self.logger.debug(f"Branch {branch_name} doesn't exist locally, skipping deletion")
                else:
                    raise

        self.logger.info(f"Creating feature branch '{branch_name}'")
        self._run_git("checkout", "-b", branch_name)

        self.logger.info(f"Successfully prepared branch '{branch_name}'")

    def checkout_branch(self, branch_name: str, create_if_missing: bool = False):
        """Checkout an existing branch or optionally create it.

        If the branch exists both locally and on origin, hard-reset the local
        branch to match origin/<branch_name>. This avoids operating on a stale
        local tip (e.g. after the remote was rewritten externally or by a
        previous Brad run), which would otherwise cause force-pushes to wipe
        out remote work.
        """
        self.logger.info(f"Checking out branch '{branch_name}'")

        existing_branches = self._run_git("branch", check=False).stdout
        branch_exists = branch_name in existing_branches

        if branch_exists:
            self._run_git("checkout", "-f", branch_name)
            if self.branch_exists_remote(branch_name):
                self._run_git("fetch", "origin", branch_name, check=False)
                self._run_git("reset", "--hard", f"origin/{branch_name}", check=False)
                self.logger.info(f"Synced local '{branch_name}' to origin/{branch_name}")
            self.logger.info(f"Checked out existing branch '{branch_name}'")
        elif create_if_missing:
            self._run_git("checkout", "-b", branch_name)
            self.logger.info(f"Created and checked out new branch '{branch_name}'")
        else:
            raise RuntimeError(f"Branch '{branch_name}' does not exist and create_if_missing=False")

    def branch_exists_remote(self, branch_name: str) -> bool:
        """Check if a branch exists on remote."""
        result = self._run_git("ls-remote", "--heads", "origin", branch_name, check=False)
        exists = bool(result.stdout.strip())
        self.logger.debug(f"Branch '{branch_name}' exists on remote: {exists}")
        return exists

    # -------------------------
    # Commit & push
    # -------------------------

    def _cleanup_llm_utility_files(self):
        """Remove LLM-generated utility files that shouldn't be in the commit."""
        import os
        import glob

        patterns = [
            "*.md",
        ]

        removed_files = []
        for pattern in patterns:
            matches = glob.glob(os.path.join(self.repo_path, "**", pattern), recursive=True)
            for file_path in matches:
                if os.path.exists(file_path):
                    try:
                        self._run_git("reset", "HEAD", file_path, check=False)
                        os.remove(file_path)
                        removed_files.append(os.path.basename(file_path))
                        self.logger.info(f"Removed LLM utility file: {os.path.basename(file_path)}")
                    except Exception as e:
                        self.logger.warning(f"Failed to remove {file_path}: {e}")

        if removed_files:
            self.logger.info(f"Cleaned up {len(removed_files)} LLM utility files: {', '.join(removed_files)}")

        return removed_files

    def commit_all(self, message: str):
        """Stage and commit all changes to tracked files only."""
        self.logger.info(f"Committing changes: {message[:50]}...")

        self._cleanup_llm_utility_files()

        self._run_git("add", "-u")

        status = self._run_git("status", "--porcelain", check=False).stdout
        if not status.strip():
            self.logger.warning("No changes to commit")
            return

        self._run_git("commit", "-m", message)
        self.logger.info("Successfully committed changes")

    def push(self, branch_name: str, force: bool = False):
        """Push branch to remote."""
        self.logger.info(f"Pushing branch '{branch_name}' to origin")

        args = ["push", "-u", "origin", branch_name]
        if force:
            args.insert(1, "--force")
            self.logger.warning("Force pushing branch")

        self._run_git(*args)
        self.logger.info(f"Successfully pushed branch '{branch_name}'")

    # -------------------------
    # Utility
    # -------------------------

    def is_clean_working_tree(self) -> bool:
        result = self._run_git("status", "--porcelain", check=False)
        return result.stdout.strip() == ""

    def get_current_branch(self) -> Optional[str]:
        """Get the name of the currently checked out branch."""
        result = self._run_git("rev-parse", "--abbrev-ref", "HEAD", check=False)
        if result.returncode != 0:
            return None
        branch = result.stdout.strip()
        self.logger.debug(f"Current branch: {branch}")
        return branch

    def get_head_commit(self, branch: str = "main") -> str:
        """Return the short SHA of the HEAD commit on the given branch (remote)."""
        result = self._run_git("rev-parse", "--short", f"origin/{branch}", check=False)
        if result.returncode != 0:
            result = self._run_git("rev-parse", "--short", branch, check=False)
        return result.stdout.strip() or "unknown"

    def rebase_branch(
        self,
        branch_name: str,
        base_branch: str = "main",
        auto_abort_on_conflict: bool = True,
    ) -> dict:
        """Rebase the given branch onto the latest base branch.

        Returns a dict with:
        - 'rebased': True if branch was rebased
        - 'up_to_date': True if already up to date
        - 'conflict': True if rebase conflicts occurred
        - 'conflicted_files': list of unmerged paths (only when conflict and
          ``auto_abort_on_conflict=False``; the rebase is left in progress so
          the caller can resolve and call :meth:`continue_rebase`)
        - 'error': error message if something else went wrong

        When ``auto_abort_on_conflict`` is True (default) the legacy behaviour
        is preserved: any conflict triggers ``git rebase --abort`` and a clean
        working tree is left behind. Set it to False to drive AI-based
        conflict resolution from the orchestrator.
        """
        self.logger.info(f"Attempting to rebase branch '{branch_name}' onto '{base_branch}'")

        try:
            # Fetch latest
            self._run_git("fetch", "origin")

            # Check if branch exists locally
            existing_branches = self._run_git("branch", check=False).stdout
            if branch_name not in existing_branches:
                # Fetch the remote branch
                self._run_git("fetch", "origin", branch_name)
                self._run_git("checkout", "-b", branch_name, f"origin/{branch_name}")
            else:
                self._run_git("checkout", branch_name)

            # Get current commit and base commit
            result = self._run_git("rev-parse", "HEAD", check=False)
            current_sha = result.stdout.strip()

            result = self._run_git("rev-parse", f"origin/{base_branch}", check=False)
            base_sha = result.stdout.strip()

            # Validate commit hashes before proceeding
            if not current_sha or len(current_sha) < 7 or not all(c in '0123456789abcdef' for c in current_sha.lower()):
                self.logger.error(f"Invalid current SHA: '{current_sha}'")
                return {"rebased": False, "error": "Invalid current commit hash"}

            if not base_sha or len(base_sha) < 7 or not all(c in '0123456789abcdef' for c in base_sha.lower()):
                self.logger.error(f"Invalid base SHA: '{base_sha}'")
                return {"rebased": False, "error": "Invalid base commit hash"}

            # Check if already up to date by seeing if base is an ancestor
            result = self._run_git("merge-base", "--is-ancestor", f"origin/{base_branch}", "HEAD", check=False)
            if result.returncode == 0:
                self.logger.info(f"Branch '{branch_name}' is already up to date with {base_branch}")
                return {"rebased": False, "up_to_date": True}

            # Attempt rebase
            self.logger.info(f"Rebasing '{branch_name}' onto 'origin/{base_branch}'")
            result = self._run_git("rebase", f"origin/{base_branch}", check=False)

            if result.returncode == 0:
                self.logger.info(f"Successfully rebased '{branch_name}'")
                # Push with force since history changed
                push_result = self._run_git("push", "origin", branch_name, "--force-with-lease", check=False)
                if push_result.returncode != 0:
                    self.logger.warning(f"Rebase succeeded but push failed: {push_result.stderr}")
                    return {"rebased": True, "push_failed": True, "error": push_result.stderr.strip()}
                return {"rebased": True, "push_failed": False}
            else:
                # Check if it's a conflict
                if "conflict" in result.stdout.lower() or "conflict" in result.stderr.lower():
                    self.logger.warning(f"Rebase conflicts on '{branch_name}'")
                    if auto_abort_on_conflict:
                        self._run_git("rebase", "--abort", check=False)
                        return {"rebased": False, "conflict": True}
                    # Leave the rebase in progress; caller will resolve.
                    conflicted = self.list_unmerged_files()
                    return {
                        "rebased": False,
                        "conflict": True,
                        "conflicted_files": conflicted,
                        "base_branch": base_branch,
                    }
                else:
                    self.logger.error(f"Rebase failed: {result.stderr}")
                    self._run_git("rebase", "--abort", check=False)
                    return {"rebased": False, "error": result.stderr}

        except Exception as e:
            self.logger.error(f"Failed to rebase branch '{branch_name}': {e}")
            # Try to abort any in-progress rebase
            self._run_git("rebase", "--abort", check=False)
            return {"rebased": False, "error": str(e)}

    # -------------------------
    # Rebase conflict helpers (used by AI-driven resolution)
    # -------------------------

    def list_unmerged_files(self) -> list:
        """Return paths of files currently in an unmerged (conflicted) state."""
        result = self._run_git(
            "diff", "--name-only", "--diff-filter=U", check=False
        )
        return [line for line in result.stdout.splitlines() if line.strip()]

    def is_rebase_in_progress(self) -> bool:
        """True if `git rebase` is mid-flight (rebase-merge or rebase-apply dir exists)."""
        git_dir = self.repo_path / ".git"
        return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()

    def continue_rebase(self) -> dict:
        """Continue an in-progress rebase after the caller resolved conflicts.

        Stages everything in the working tree first (the AI agent edits files
        but is told not to run git), then runs ``git rebase --continue``.

        Returns the same shape as :meth:`rebase_branch`.
        """
        self.logger.info("Continuing in-progress rebase")
        # Stage anything the resolver touched. ``git add -A`` is intentional:
        # the AI may have created/deleted files as part of the resolution.
        self._run_git("add", "-A", check=False)

        env_extra = {"GIT_EDITOR": "true"}  # auto-accept commit message
        result = subprocess.run(
            ["git", "-C", str(self.repo_path), "rebase", "--continue"],
            capture_output=True,
            text=True,
            env={**os.environ, **env_extra},
        )
        if result.returncode == 0:
            if self.is_rebase_in_progress():
                # Multi-commit rebase that produced no further conflicts on
                # this step but isn't finished yet. Caller should loop.
                conflicted = self.list_unmerged_files()
                if conflicted:
                    return {"rebased": False, "conflict": True, "conflicted_files": conflicted}
                # Still in progress with nothing to do — try once more.
                return {"rebased": False, "conflict": False, "in_progress": True}
            self.logger.info("Rebase completed")
            return {"rebased": True}

        combined = (result.stdout or "") + (result.stderr or "")
        if "conflict" in combined.lower():
            self.logger.warning("New conflicts surfaced after --continue")
            return {
                "rebased": False,
                "conflict": True,
                "conflicted_files": self.list_unmerged_files(),
            }
        self.logger.error(f"git rebase --continue failed: {combined}")
        return {"rebased": False, "error": combined.strip()}

    def abort_rebase(self) -> None:
        """Abort an in-progress rebase. No-op if none."""
        if self.is_rebase_in_progress():
            self.logger.info("Aborting in-progress rebase")
        self._run_git("rebase", "--abort", check=False)

    def force_push_with_lease(self, branch_name: str) -> dict:
        """Force-push ``branch_name`` to origin with ``--force-with-lease``.

        Never call this for ``main``/protected base branches — the caller is
        responsible for ensuring ``branch_name`` is a feature branch.
        """
        if branch_name in {"main", "master"}:
            raise ValueError(
                f"Refusing to force-push protected branch '{branch_name}'"
            )
        result = self._run_git(
            "push", "origin", branch_name, "--force-with-lease", check=False
        )
        if result.returncode != 0:
            self.logger.warning(f"force-with-lease push failed: {result.stderr}")
            return {"pushed": False, "error": result.stderr.strip()}
        self.logger.info(f"Force-pushed '{branch_name}' to origin (with lease)")
        return {"pushed": True}

    def read_conflicted_file(self, path: str) -> str:
        """Read the working-copy content of a conflicted file (markers included)."""
        fp = self.repo_path / path
        if not fp.exists():
            return ""
        try:
            return fp.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            self.logger.warning(f"Could not read conflicted file {path}: {e}")
            return ""

    def show_stage_blob(self, stage: int, path: str) -> str:
        """Return the contents of one stage of an unmerged file.

        Stages: 1 = merge base, 2 = ours (HEAD/feature), 3 = theirs (incoming/main).
        Returns "" if the stage doesn't exist (e.g. add/add conflicts have no stage 1).
        """
        if stage not in (1, 2, 3):
            raise ValueError("stage must be 1, 2, or 3")
        result = self._run_git("show", f":{stage}:{path}", check=False)
        if result.returncode != 0:
            return ""
        return result.stdout

    def blame_range(self, ref: str, path: str, start_line: int, end_line: int) -> str:
        """Return ``git blame`` output for a line range at the given ref.

        Best-effort: returns "" on any failure (e.g. file doesn't exist at ref).
        """
        if start_line < 1 or end_line < start_line:
            return ""
        result = self._run_git(
            "blame", "-L", f"{start_line},{end_line}", ref, "--", path, check=False
        )
        if result.returncode != 0:
            return ""
        return result.stdout

    def log_messages(self, ref_range: str, path: str, max_commits: int = 5) -> str:
        """Return commit messages touching ``path`` over ``ref_range``.

        Best-effort: returns "" on failure.
        """
        result = self._run_git(
            "log", f"-n{max_commits}", "--format=%H%n%s%n%b%n---", ref_range,
            "--", path, check=False,
        )
        if result.returncode != 0:
            return ""
        return result.stdout

    def reset_to_clean_state(self, branch: str = "main"):
        """Reset repository to clean state on specified branch."""
        self.logger.info(f"Resetting to clean state on {branch}")

        self._run_git("merge", "--abort", check=False)
        self._run_git("rebase", "--abort", check=False)
        self._run_git("cherry-pick", "--abort", check=False)

        self._run_git("fetch", "origin")
        self._run_git("checkout", "-f", branch)
        self._run_git("reset", "--hard", f"origin/{branch}")

        self.logger.info(f"Repository reset to clean state on {branch}")
