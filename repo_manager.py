import subprocess
from pathlib import Path
from typing import Optional
from logging_config import get_logger


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
        """Prepare a feature branch from base branch."""
        self.logger.info(f"Preparing branch '{branch_name}' from '{base_branch}'")
        
        # 1. Ensure base is up-to-date
        self.logger.info(f"Fetching latest changes from origin")
        self._run_git("fetch", "origin")
        
        self.logger.info(f"Checking out {base_branch}")
        self._run_git("checkout", base_branch)
        
        self.logger.info(f"Resetting to origin/{base_branch}")
        self._run_git("reset", "--hard", f"origin/{base_branch}")

        # 2. Delete local branch if exists
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

        # 3. Create feature branch
        self.logger.info(f"Creating feature branch '{branch_name}'")
        self._run_git("checkout", "-b", branch_name)
        
        self.logger.info(f"Successfully prepared branch '{branch_name}'")
    
    def checkout_branch(self, branch_name: str, create_if_missing: bool = False):
        """Checkout an existing branch or optionally create it."""
        self.logger.info(f"Checking out branch '{branch_name}'")
        
        # Check if branch exists locally
        existing_branches = self._run_git("branch", check=False).stdout
        branch_exists = branch_name in existing_branches
        
        if branch_exists:
            self._run_git("checkout", branch_name)
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
        
        # Only add modified tracked files, not untracked files
        # Use -u flag to update tracked files only
        self._run_git("add", "-u")
        
        # Check if there are changes to commit
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
