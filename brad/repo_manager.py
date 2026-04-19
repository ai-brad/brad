"""Git repository operations manager."""
import subprocess
from pathlib import Path
from typing import Optional
from brad.logging_config import get_logger


class RepoManager:
    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.repo_path = Path(cfg.target_repo_path)
        self.logger.info(f"Initialized repo manager for {self.repo_path}")

        if not self.repo_path.exists():
            raise ValueError(f"Repository path does not exist: {self.repo_path}")
        if not (self.repo_path / ".git").exists():
            raise ValueError(f"Not a git repository: {self.repo_path}")

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
        """Checkout an existing branch or optionally create it."""
        self.logger.info(f"Checking out branch '{branch_name}'")

        existing_branches = self._run_git("branch", check=False).stdout
        branch_exists = branch_name in existing_branches

        if branch_exists:
            self._run_git("checkout", "-f", branch_name)
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

    def commit_all(self, message: str):
        """Stage and commit all changes to tracked files only."""
        self.logger.info(f"Committing changes: {message[:50]}...")

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

    def rebase_branch(self, branch_name: str, base_branch: str = "main") -> dict:
        """Rebase the given branch onto the latest base branch.
        
        Returns a dict with:
        - 'rebased': True if branch was rebased
        - 'up_to_date': True if already up to date
        - 'conflict': True if rebase conflicts occurred
        - 'error': error message if something else went wrong
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
                    self._run_git("rebase", "--abort", check=False)
                    return {"rebased": False, "conflict": True}
                else:
                    self.logger.error(f"Rebase failed: {result.stderr}")
                    self._run_git("rebase", "--abort", check=False)
                    return {"rebased": False, "error": result.stderr}
                    
        except Exception as e:
            self.logger.error(f"Failed to rebase branch '{branch_name}': {e}")
            # Try to abort any in-progress rebase
            self._run_git("rebase", "--abort", check=False)
            return {"rebased": False, "error": str(e)}

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
