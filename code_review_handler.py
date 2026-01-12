"""
Code Review Handler - Monitors and responds to PR review comments
"""

from typing import List, Dict
from logging_config import get_logger
from config import Config
from github_client import GitHubClient
from repo_manager import RepoManager
from ai_agent_interface import create_ai_agent_interface


class CodeReviewHandler:
    """Handles code review comments on Brad's PRs."""
    
    def __init__(self, cfg: Config):
        self.logger = get_logger(__name__)
        self.cfg = cfg
        self.github = GitHubClient(cfg)
        self.repo = RepoManager(cfg)
        self.agent = create_ai_agent_interface(cfg)
    
    def process_review_comments(self) -> int:
        """
        Check all Brad PRs for new review comments and process them.
        Returns: Number of comments processed.
        """
        self.logger.info("Checking for review comments on Brad's PRs")
        
        brad_prs = self.github.get_brad_prs()
        if not brad_prs:
            self.logger.info("No Brad PRs found")
            return 0
        
        total_processed = 0
        for pr in brad_prs:
            pr_number = pr['number']
            pr_title = pr['title']
            branch_name = pr['head']['ref']
            
            self.logger.info(f"Checking PR #{pr_number}: {pr_title}")
            
            comments = self.github.get_review_comments_needing_response(pr_number)
            if not comments:
                self.logger.debug(f"No new comments on PR #{pr_number}")
                continue
            
            self.logger.info(f"Found {len(comments)} unresponded comments on PR #{pr_number}")
            
            for comment in comments:
                try:
                    self._process_single_comment(pr_number, branch_name, comment)
                    total_processed += 1
                except Exception as e:
                    self.logger.error(f"Failed to process comment {comment['id']}: {e}")
                    continue
        
        return total_processed
    
    def _process_single_comment(self, pr_number: int, branch_name: str, comment: Dict):
        """Process a single review comment."""
        comment_id = comment['id']
        comment_body = comment.get('body', '')
        file_path = comment.get('path', '')
        line_number = comment.get('line', comment.get('original_line', ''))
        
        self.logger.info(
            f"Processing comment {comment_id} on PR #{pr_number}: "
            f"{file_path}:{line_number}"
        )
        
        # Step 1: Immediately reply "Brad checking" to claim the comment
        try:
            self.github.reply_to_review_comment(
                pr_number, 
                comment_id, 
                "Brad checking"
            )
            self.logger.info(f"Replied 'Brad checking' to comment {comment_id}")
        except Exception as e:
            self.logger.error(f"Failed to reply to comment: {e}")
            raise
        
        # Step 2: Prepare the context for the AI agent
        context = self._build_review_context(pr_number, branch_name, comment)
        
        # Step 3: Checkout the branch and sync with remote
        self.logger.info(f"Checking out branch {branch_name}")
        self.repo.checkout_branch(branch_name)
        
        # Sync with remote: abort any ongoing operations, reset to remote state
        try:
            # Abort any ongoing merge/rebase
            self.repo._run_git("merge", "--abort", check=False)
            self.repo._run_git("rebase", "--abort", check=False)
            
            # Fetch latest from remote
            self.repo._run_git("fetch", "origin", branch_name)
            
            # Reset hard to remote state
            self.repo._run_git("reset", "--hard", f"origin/{branch_name}")
            self.logger.info(f"Synced with origin/{branch_name}")
        except Exception as e:
            self.logger.warning(f"Could not sync with remote: {e}")
        
        # Step 4: Ask AI agent to address the review comment
        prompt = self._build_review_prompt(context)
        self.logger.info(f"Asking AI agent to address review comment")
        
        try:
            # Use the AI agent interface's invoke method
            result = self.agent.invoke_review_fix(
                pr_number=pr_number,
                comment_id=comment_id,
                comment_body=comment_body,
                file_path=file_path,
                line_number=line_number,
                repo_path=self.cfg.target_repo_path,
                branch_name=branch_name
            )
            
            if result.get('action') == 'error':
                raise Exception(result.get('message', 'Unknown error'))
            
            self.logger.info("AI agent completed review fix")
            
            # Step 5: Push changes (force push to avoid conflicts)
            self.logger.info("Pushing review fix")
            try:
                self.repo.push(branch_name, force=False)
            except Exception as e:
                self.logger.warning(f"Normal push failed: {e}. Trying force push.")
                self.repo.push(branch_name, force=True)
            
            self.logger.info(f"Successfully addressed review comment {comment_id}")
            
        except Exception as e:
            self.logger.error(f"AI agent failed to address review: {e}")
            # Reply with error
            try:
                self.github.reply_to_review_comment(
                    pr_number,
                    comment_id,
                    f"Brad encountered an error while processing this review comment: {str(e)}"
                )
            except:
                pass
            raise
    
    def _build_review_context(self, pr_number: int, branch_name: str, comment: Dict) -> Dict:
        """Build context information for the review comment."""
        pr_info = self.github.get_pr(pr_number)
        
        return {
            'pr_number': pr_number,
            'pr_title': pr_info.get('title', ''),
            'pr_body': pr_info.get('body', ''),
            'branch_name': branch_name,
            'comment_id': comment['id'],
            'comment_body': comment.get('body', ''),
            'file_path': comment.get('path', ''),
            'line_number': comment.get('line', comment.get('original_line', '')),
            'diff_hunk': comment.get('diff_hunk', ''),
        }
    
    def _build_review_prompt(self, context: Dict) -> str:
        """Build the prompt for the AI agent to address the review comment."""
        prompt = f"""You are reviewing and addressing a code review comment on PR #{context['pr_number']}.

PR Title: {context['pr_title']}
Branch: {context['branch_name']}

Review Comment Location:
File: {context['file_path']}
Line: {context['line_number']}

Diff Context:
{context['diff_hunk']}

Review Comment:
{context['comment_body']}

Please address this review comment by making the necessary code changes. 
The repository is already checked out to the correct branch ({context['branch_name']}).

After making your changes, they will be automatically committed and pushed.
"""
        return prompt
