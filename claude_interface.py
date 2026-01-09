import subprocess
import json
from typing import Dict, List, Optional
from pathlib import Path
from logging_config import get_logger


class ClaudeInterface:
    """
    Interface to Claude CLI for autonomous software engineering tasks.
    Invokes fresh Claude CLI sessions with complete context.
    """
    
    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.claude_cli_path = cfg.claude_cli_path
        self.timeout = 1800  # 30 minutes
        self.logger.info(f"Initialized Claude CLI interface: {self.claude_cli_path}")
    
    def invoke_requirements_analysis(
        self,
        issue_key: str,
        description: str,
        attachment_paths: List[str],
        repo_path: str,
        iteration: int
    ) -> Dict:
        """
        Invoke Claude CLI for requirements analysis phase.
        
        Returns dict with:
        - action: "clarify" | "propose_scenarios" | "implement" | "error"
        - message: text to post to JIRA
        - details: additional context
        """
        self.logger.info(f"Invoking Claude CLI for requirements analysis: {issue_key}")
        
        prompt = self._build_requirements_prompt(
            issue_key, description, attachment_paths, iteration
        )
        
        return self._invoke_claude(prompt, repo_path, phase="requirements_analysis")
    
    def invoke_implementation(
        self,
        issue_key: str,
        description: str,
        attachment_paths: List[str],
        repo_path: str,
        branch_name: str,
        iteration: int
    ) -> Dict:
        """
        Invoke Claude CLI for implementation phase.
        
        Returns dict with:
        - action: "success" | "stuck" | "error"
        - pr_number: PR number if created
        - pr_url: PR URL if created
        - message: status message
        """
        self.logger.info(f"Invoking Claude CLI for implementation: {issue_key}")
        
        prompt = self._build_implementation_prompt(
            issue_key, description, attachment_paths, branch_name, iteration
        )
        
        return self._invoke_claude(prompt, repo_path, phase="implementation")
    
    def invoke_ci_fix(
        self,
        issue_key: str,
        description: str,
        ci_logs: str,
        failed_jobs: List[str],
        repo_path: str,
        branch_name: str,
        pr_number: int,
        iteration: int
    ) -> Dict:
        """
        Invoke Claude CLI for CI failure fix phase.
        
        Returns dict with:
        - action: "fixed" | "stuck" | "error"
        - message: status message
        """
        self.logger.info(f"Invoking Claude CLI for CI fix: {issue_key} (iteration {iteration})")
        
        prompt = self._build_ci_fix_prompt(
            issue_key, description, ci_logs, failed_jobs, pr_number, iteration
        )
        
        return self._invoke_claude(prompt, repo_path, phase="ci_fix")
    
    def _build_requirements_prompt(
        self,
        issue_key: str,
        description: str,
        attachment_paths: List[str],
        iteration: int
    ) -> str:
        """Build prompt for requirements analysis phase."""
        attachments_text = ""
        if attachment_paths:
            attachments_text = "\n\nAttachments available:\n" + "\n".join(
                f"- {Path(p).name}: {p}" for p in attachment_paths
            )
        
        return f"""You are Brad, an autonomous senior software engineer. This is a requirements analysis task.

JIRA Issue: {issue_key}
Iteration: {iteration}

Issue Description:
{description}
{attachments_text}

Your task:
1. Analyze the requirements for completeness and clarity
2. Check against this checklist:
   - Expected behavior is clearly described
   - Non-goals / out-of-scope behavior is stated or inferable
   - Error and edge cases are described
   - Impact on existing functionality is specified
   - Backward compatibility expectations are clear
   - Performance or scale constraints are stated or explicitly irrelevant
   - Acceptance criteria or test scenarios can be derived

3. Determine the appropriate action:
   - If requirements are incomplete or ambiguous: Prepare specific clarification questions
   - If requirements are complete but scenarios not explicit: Propose concrete test scenarios (Given/When/Then format)
   - If everything is clear: Indicate ready for implementation

Output Format (JSON):
{{
    "action": "clarify" | "propose_scenarios" | "ready",
    "message": "Text to post as JIRA comment",
    "details": "Any additional context"
}}

Rules:
- Be specific and actionable in clarification requests
- Propose scenarios that are testable and concrete
- Do not invent requirements
- Be conservative - clarify if uncertain
"""
    
    def _build_implementation_prompt(
        self,
        issue_key: str,
        description: str,
        attachment_paths: List[str],
        branch_name: str,
        iteration: int
    ) -> str:
        """Build prompt for implementation phase."""
        attachments_text = ""
        if attachment_paths:
            attachments_text = "\n\nAttachments:\n" + "\n".join(
                f"- {Path(p).name}: {p}" for p in attachment_paths
            )
        
        return f"""You are Brad, an autonomous senior software engineer. This is an implementation task.

JIRA Issue: {issue_key}
Branch: {branch_name}
Iteration: {iteration}

Issue Description:
{description}
{attachments_text}

Your task:
1. Implement the requirements in the current repository
2. Write comprehensive tests (unit, integration, AI tests as appropriate)
3. Run all tests locally until they pass
4. Commit all changes with message: "{issue_key}: <descriptive message>"
5. Push the branch to origin
6. Create a pull request against main branch

Important:
- You have FULL access to git operations (commit, push)
- You have access to GitHub CLI or API to create PR
- Follow existing code style exactly
- Ensure all tests pass before pushing
- The commit message MUST reference {issue_key}

Output Format (JSON):
{{
    "action": "success" | "stuck",
    "pr_number": <number if created, else null>,
    "pr_url": "<url if created, else null>",
    "message": "Status message for JIRA"
}}

If you encounter insurmountable issues, return action="stuck" with details.
"""
    
    def _build_ci_fix_prompt(
        self,
        issue_key: str,
        description: str,
        ci_logs: str,
        failed_jobs: List[str],
        pr_number: int,
        iteration: int
    ) -> str:
        """Build prompt for CI fix phase."""
        return f"""You are Brad, an autonomous senior software engineer. This is a CI failure fix task.

JIRA Issue: {issue_key}
PR Number: #{pr_number}
CI Fix Iteration: {iteration}

Original Issue Description:
{description}

CI Failure Details:
Failed Jobs: {', '.join(failed_jobs)}

CI Logs:
{ci_logs}

Your task:
1. Analyze the CI failure logs
2. Identify the root cause
3. Fix the issues in the code
4. Run tests locally to verify the fix
5. Commit the fix with message: "{issue_key}: Fix CI - <what was fixed>"
6. Push the changes (this will automatically re-trigger CI)

Important:
- You are already on the correct branch
- The PR already exists - just push your fixes
- Focus on fixing the actual issue, not masking it
- Ensure tests pass locally before pushing

Output Format (JSON):
{{
    "action": "fixed" | "stuck",
    "message": "Explanation of what was fixed or why stuck"
}}
"""
    
    def _invoke_claude(self, prompt: str, repo_path: str, phase: str) -> Dict:
        """
        Invoke Claude CLI with the given prompt in the specified repository.
        
        Returns parsed JSON response from Claude.
        """
        self.logger.info(f"Invoking Claude CLI (phase: {phase})")
        self.logger.debug(f"Repository: {repo_path}")
        
        try:
            # Claude CLI command: claude --project <path> --prompt <prompt>
            # We'll use stdin to pass the prompt
            cmd = [
                self.claude_cli_path,
                "code",
                "--project", repo_path,
                "--dangerously-skip-approval",  # For automation
            ]
            
            self.logger.debug(f"Running: {' '.join(cmd)}")
            
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=repo_path,
            )
            
            if result.returncode != 0:
                self.logger.error(f"Claude CLI failed with code {result.returncode}")
                self.logger.error(f"stderr: {result.stderr}")
                return {
                    "action": "error",
                    "message": f"Claude CLI failed: {result.stderr[:500]}",
                    "details": result.stderr
                }
            
            # Parse JSON response from stdout
            output = result.stdout.strip()
            self.logger.debug(f"Claude output (first 500 chars): {output[:500]}")
            
            # Try to extract JSON from output (Claude might add extra text)
            json_start = output.find("{")
            json_end = output.rfind("}") + 1
            
            if json_start >= 0 and json_end > json_start:
                json_str = output[json_start:json_end]
                response = json.loads(json_str)
                self.logger.info(f"Parsed response action: {response.get('action')}")
                return response
            else:
                self.logger.error("No JSON found in Claude output")
                return {
                    "action": "error",
                    "message": "Could not parse Claude response",
                    "details": output[:1000]
                }
        
        except subprocess.TimeoutExpired:
            self.logger.error(f"Claude CLI timed out after {self.timeout}s")
            return {
                "action": "error",
                "message": f"Claude CLI timed out after {self.timeout}s",
                "details": "Timeout"
            }
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse JSON response: {e}")
            return {
                "action": "error",
                "message": f"Invalid JSON response from Claude: {e}",
                "details": output[:1000] if 'output' in locals() else "N/A"
            }
        except Exception as e:
            self.logger.error(f"Unexpected error invoking Claude: {e}", exc_info=True)
            return {
                "action": "error",
                "message": f"Unexpected error: {str(e)}",
                "details": str(e)
            }
