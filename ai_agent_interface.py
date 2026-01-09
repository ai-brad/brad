import subprocess
import json
from typing import Dict, List, Optional
from pathlib import Path
from logging_config import get_logger
from abc import ABC, abstractmethod


class AIAgentInterface(ABC):
    """
    Abstract interface for AI coding agents.
    Invokes fresh AI agent sessions with complete context.
    """
    
    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.timeout = 1800  # 30 minutes
    
    @abstractmethod
    def _invoke_agent(self, prompt: str, repo_path: str, phase: str, attachment_paths: List[str] = None) -> Dict:
        """Invoke the AI agent with the given prompt."""
        pass
    
    def invoke_requirements_analysis(
        self,
        issue_key: str,
        description: str,
        attachment_paths: List[str],
        repo_path: str,
        iteration: int
    ) -> Dict:
        """
        Invoke AI agent for requirements analysis phase.
        
        Returns dict with:
        - action: "clarify" | "propose_scenarios" | "implement" | "error"
        - message: text to post to JIRA
        - details: additional context
        """
        self.logger.info(f"Invoking AI agent for requirements analysis: {issue_key}")
        
        prompt = self._build_requirements_prompt(
            issue_key, description, attachment_paths, iteration
        )
        
        return self._invoke_agent(prompt, repo_path, phase="requirements_analysis", attachment_paths=attachment_paths)
    
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
        Invoke AI agent for implementation phase.
        
        Returns dict with:
        - action: "success" | "stuck" | "error"
        - pr_number: PR number if created
        - pr_url: PR URL if created
        - message: status message
        """
        self.logger.info(f"Invoking AI agent for implementation: {issue_key}")
        
        prompt = self._build_implementation_prompt(
            issue_key, description, attachment_paths, branch_name, iteration
        )
        
        return self._invoke_agent(prompt, repo_path, phase="implementation", attachment_paths=attachment_paths)
    
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
        Invoke AI agent for CI failure fix phase.
        
        Returns dict with:
        - action: "fixed" | "stuck" | "error"
        - message: status message
        """
        self.logger.info(f"Invoking AI agent for CI fix: {issue_key} (iteration {iteration})")
        
        prompt = self._build_ci_fix_prompt(
            issue_key, description, ci_logs, failed_jobs, pr_number, iteration
        )
        
        return self._invoke_agent(prompt, repo_path, phase="ci_fix")
    
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
        
        return f"""You are Brad, an autonomous senior software engineer analyzing JIRA issue {issue_key}.

Issue Description:
{description}
{attachments_text}

CRITICAL INSTRUCTIONS:
1. **FIRST**: Search and analyze the codebase thoroughly to understand current behavior and implementation
   - This analysis is INTERNAL ONLY
   - Do NOT output "I'll analyze...", "Let me check...", "Now I need to understand..."
   - Do NOT narrate your search process

2. **THEN**: Translate your technical findings into BUSINESS/FUNCTIONAL language for the PM

3. **RESPOND** by starting your output IMMEDIATELY with ONE of these formats:
   - "CLARIFYING QUESTIONS:" followed by the questions
   - "ACCEPTANCE CRITERIA:" followed by the scenarios
   - "READY TO IMPLEMENT" followed by the summary
   
   **DO NOT** include:
   - "I'll analyze the codebase..."
   - "Let me search for..."
   - "Based on my analysis..."
   - Any narration of what you're doing or thinking
   
   **JUST START** with the output format directly.

   a) **"CLARIFYING QUESTIONS:"** - If there are genuine functional/business gaps
      - Start with PM instructions explaining what to do
      - Then list questions that need clarification
      
      Format EXACTLY like this:
      ```
      Hi! I've analyzed the codebase and have some questions about the requirements that need clarification before I can implement this feature.
      
      **Please update the JIRA description with answers to these questions:**
      
      CLARIFYING QUESTIONS:
      
      1. [Question about business logic]
      2. [Question about edge case]
      ```
      
      ✅ DO:
      - Write for a semi-technical Product Manager (NOT developers)
      - Use business terms: "guest", "booking", "request", "change", "cancellation"
      - Ask about user flows, edge cases, business rules, observable behavior
      
      ❌ DON'T:
      - Mention variable names, code constants, file names, or technical jargon
      
      Example GOOD question:
      "When a guest who booked through Booking.com contacts us directly to change their reservation, should we handle it ourselves or redirect them back to Booking.com?"

   b) **"ACCEPTANCE CRITERIA:"** - If requirements are mostly clear (USE THIS OPTION WHEN POSSIBLE)
      - Start with PM instructions explaining what to do with these criteria
      - Then write Given/When/Then scenarios describing OBSERVABLE business behavior
      - Use language a non-technical stakeholder would understand
      - Focus on user actions and system responses, NOT implementation
      - Cover all major scenarios and edge cases you discovered in the code
      
      Format EXACTLY like this:
      ```
      Hi! I've analyzed the codebase and understand the feature requirements. Below are the acceptance criteria I'll use to implement this feature.
      
      **Please review and validate these scenarios.** If they match your requirements, I'll proceed with implementation. If anything needs adjustment, please update the JIRA description and I'll revise my approach.
      
      ACCEPTANCE CRITERIA:
      
      GIVEN a guest booked through Booking.com
      WHEN the guest emails us directly to request a date change
      THEN we should process the change ourselves without redirecting to Booking.com
      
      GIVEN we receive a notification from Booking.com about a guest change
      WHEN the change was initiated through Booking.com's platform
      THEN we should NOT treat it as a direct guest request
      ```

   c) **"READY TO IMPLEMENT"** - Only if everything is crystal clear
      - Summarize understanding at BUSINESS level only
      - NO technical details whatsoever
      - Example: "I understand we need to differentiate between guests who contact us directly versus changes that come through the booking platform, and only redirect the direct contacts."

AUDIENCE: Product Manager who understands the domain but NOT the code. Zero code references allowed.
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
        
        return f"""You are Brad, an autonomous software engineer implementing JIRA issue {issue_key}.

Branch: {branch_name}
Iteration: {iteration}

Requirements:
{description}
{attachments_text}

Tasks:
1. Implement the feature according to requirements
2. Write tests (unit tests mandatory, integration tests if needed)
3. Ensure all tests pass locally
4. Commit with message: "{issue_key}: <concise description>"
5. Push the branch to origin
6. Create a pull request against main branch with title "{issue_key}: <description>"

Important:
- Follow existing code style exactly
- Ensure all tests pass before pushing
- You have full access to git operations and GitHub CLI/API

After completing the work, respond with a status update formatted for JIRA:

If SUCCESSFUL:
- Mention "Created PR #<number>" or include the PR URL in your response
- List the key changes and assumptions made during implementation
- Keep it concise and professional

If STUCK or BLOCKED:
- Clearly state what blocked you and why
- Use words like "stuck", "cannot", or "unable to"
- Suggest what's needed to unblock

Your response will be posted directly as a JIRA comment. Be clear and actionable.
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
        return f"""You are Brad, fixing CI failures for JIRA issue {issue_key} (PR #{pr_number}).

Iteration: {iteration}

Original Requirements:
{description}

Failed CI Jobs:
{', '.join(failed_jobs)}

CI Logs:
{ci_logs}

Tasks:
1. Analyze the CI failure logs
2. Identify and fix the root cause
3. Run tests locally to verify
4. Commit with message: "{issue_key}: Fix CI - <what was fixed>"
5. Push changes (will re-trigger CI automatically)

Important:
- You're already on the correct branch
- The PR exists - just push your fixes
- Fix the actual issue, don't mask it

After fixing, respond with a status update formatted for JIRA:

If FIXED:
- Use words like "fixed", "resolved", or "corrected"
- Explain what was wrong and what you changed
- Be concise and clear

If STUCK:
- Explain what you tried and why it didn't work
- State clearly that you're stuck

Your response will be posted directly as a JIRA comment.
"""


class ClaudeCodeInterface(AIAgentInterface):
    """Interface to Claude CLI (claude code) for autonomous software engineering tasks."""
    
    def __init__(self, cfg):
        super().__init__(cfg)
        self.claude_cli_path = cfg.claude_cli_path
        self.logger.info(f"Initialized Claude Code interface: {self.claude_cli_path}")
    
    def _invoke_agent(self, prompt: str, repo_path: str, phase: str, attachment_paths: List[str] = None) -> Dict:
        """Invoke Claude CLI with the given prompt in the specified repository."""
        self.logger.info(f"Invoking Claude CLI (phase: {phase})")
        self.logger.debug(f"Repository: {repo_path}")
        
        try:
            cmd = [
                self.claude_cli_path,
                "code",
                "--project", repo_path,
                "--dangerously-skip-approval",
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
            
            output = result.stdout.strip()
            self.logger.debug(f"Claude output (first 500 chars): {output[:500]}")
            
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


class OpenCodeInterface(AIAgentInterface):
    """Interface to OpenCode CLI for autonomous software engineering tasks."""
    
    def __init__(self, cfg):
        super().__init__(cfg)
        self.opencode_cli_path = cfg.opencode_cli_path
        self.logger.info(f"Initialized OpenCode interface: {self.opencode_cli_path}")
    
    def _parse_conversational_response(self, output: str, phase: str) -> Dict:
        """Parse conversational OpenCode output into structured response."""
        import re
        
        # Strip unwanted analysis narration (but preserve PM instructions that start with "Hi!")
        # Only strip if output starts with analysis phrases
        unwanted_starts = [
            "I'll analyze", "Let me analyze", "Let me check", "Let me search",
            "Now let me", "Based on my analysis", "I've analyzed the codebase and"
        ]
        
        lines = output.split('\n')
        start_idx = 0
        for i, line in enumerate(lines):
            line_stripped = line.strip()
            # Skip empty lines and unwanted analysis narration
            if not line_stripped:
                start_idx = i + 1
                continue
            # If we hit a line that starts with analysis, skip it
            if any(line_stripped.startswith(phrase) for phrase in unwanted_starts):
                start_idx = i + 1
                continue
            # Once we hit meaningful content (like "Hi!" or "ACCEPTANCE CRITERIA:"), stop skipping
            break
        
        if start_idx > 0:
            self.logger.debug(f"Stripped {start_idx} lines of analysis narration")
            output = '\n'.join(lines[start_idx:])
        
        output_lower = output.lower()
        
        # Extract PR information using regex
        pr_match = re.search(r'pr\s*#?(\d+)', output_lower)
        pr_url_match = re.search(r'(https://github\.com/[^\s]+/pull/\d+)', output, re.IGNORECASE)
        
        if phase == "requirements_analysis":
            # Look for clarifying questions
            if any(keyword in output_lower for keyword in [
                'clarifying questions', 'questions:', 'need to know', 
                'please clarify', 'unclear', 'need clarification'
            ]):
                self.logger.info("Detected clarification needed")
                return {"action": "clarify", "message": output, "details": ""}
            
            # Look for acceptance criteria
            elif any(keyword in output_lower for keyword in [
                'acceptance criteria', 'given', 'when', 'then',
                'test scenario', 'acceptance test'
            ]):
                self.logger.info("Detected acceptance criteria proposal")
                return {"action": "propose_scenarios", "message": output, "details": ""}
            
            # Look for ready signals
            elif any(keyword in output_lower for keyword in [
                'ready to implement', 'ready for implementation', 
                'clear to proceed', 'requirements are clear', 'can proceed'
            ]):
                self.logger.info("Detected ready for implementation")
                return {"action": "ready", "message": output, "details": ""}
            
            # Default to propose scenarios
            else:
                self.logger.info("Defaulting to propose scenarios")
                return {"action": "propose_scenarios", "message": output, "details": ""}
        
        elif phase == "implementation":
            # Look for PR creation
            if pr_match or pr_url_match or 'created pr' in output_lower or 'pull request' in output_lower:
                pr_number = int(pr_match.group(1)) if pr_match else None
                pr_url = pr_url_match.group(1) if pr_url_match else None
                self.logger.info(f"Detected PR creation: #{pr_number} at {pr_url}")
                return {
                    "action": "success",
                    "pr_number": pr_number,
                    "pr_url": pr_url,
                    "message": output
                }
            
            # Look for stuck/blocked signals
            elif any(keyword in output_lower for keyword in [
                'stuck', 'cannot', 'unable to', 'blocked', 'failed to'
            ]):
                self.logger.info("Detected stuck/blocked state")
                return {"action": "stuck", "message": output, "pr_number": None, "pr_url": None}
            
            # Work in progress
            else:
                self.logger.info("Detected work in progress")
                return {"action": "in_progress", "message": output, "pr_number": None, "pr_url": None}
        
        elif phase == "ci_fix":
            # Look for fix completion
            if any(keyword in output_lower for keyword in [
                'fixed', 'resolved', 'corrected', 'should pass now', 'tests passing'
            ]):
                self.logger.info("Detected CI fix completed")
                return {"action": "fixed", "message": output}
            
            # Stuck on CI fix
            else:
                self.logger.info("Detected stuck on CI fix")
                return {"action": "stuck", "message": output}
        
        # Fallback
        self.logger.warning(f"Could not categorize response for phase {phase}")
        return {"action": "error", "message": output, "details": "Could not categorize response"}
    
    def _invoke_agent(self, prompt: str, repo_path: str, phase: str, attachment_paths: List[str] = None) -> Dict:
        """Invoke OpenCode CLI with the given prompt in the specified repository."""
        import tempfile
        import os
        
        self.logger.info(f"Invoking OpenCode CLI (phase: {phase})")
        self.logger.debug(f"Repository: {repo_path}")
        self.logger.debug(f"Prompt preview: {prompt[:200]}...")
        
        # Write prompt to temp file
        prompt_file = None
        try:
            fd, prompt_file = tempfile.mkstemp(suffix='.txt', text=True, prefix='brad_prompt_')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(prompt)
            
            self.logger.debug(f"Wrote prompt to: {prompt_file}")
            
            # Build PowerShell command that reads prompt from file and passes to opencode
            # Use PowerShell to properly handle multi-line strings
            ps_cmd = f'Get-Content "{prompt_file}" -Raw | & "{self.opencode_cli_path}" run "{repo_path}" --model opencode/minimax-m2.1-free'
            
            # Add attachments
            if attachment_paths:
                for attachment_path in attachment_paths:
                    ps_cmd += f' -f "{attachment_path}"'
                    self.logger.debug(f"Adding attachment: {attachment_path}")
            
            # Add the prompt argument (reading from temp file content)
            ps_cmd += f' (Get-Content "{prompt_file}" -Raw)'
            
            self.logger.debug(f"Running PowerShell command")
            
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=repo_path,
            )
            
            if result.returncode != 0:
                self.logger.error(f"OpenCode CLI failed with code {result.returncode}")
                self.logger.error(f"stderr: {result.stderr}")
                return {
                    "action": "error",
                    "message": f"OpenCode CLI failed: {result.stderr[:500]}",
                    "details": result.stderr
                }
            
            output = result.stdout.strip()
            self.logger.debug(f"OpenCode output (first 500 chars): {output[:500]}")
            
            # Parse conversational output based on phase
            return self._parse_conversational_response(output, phase)
        
        except subprocess.TimeoutExpired:
            self.logger.error(f"OpenCode CLI timed out after {self.timeout}s")
            return {
                "action": "error",
                "message": f"OpenCode CLI timed out after {self.timeout}s",
                "details": "Timeout"
            }
        except Exception as e:
            self.logger.error(f"Unexpected error invoking OpenCode: {e}", exc_info=True)
            return {
                "action": "error",
                "message": f"Unexpected error: {str(e)}",
                "details": str(e)
            }
        finally:
            # Clean up temp file
            if prompt_file and os.path.exists(prompt_file):
                try:
                    os.remove(prompt_file)
                    self.logger.debug(f"Cleaned up prompt file: {prompt_file}")
                except Exception as e:
                    self.logger.debug(f"Failed to clean up prompt file: {e}")


def create_ai_agent_interface(cfg) -> AIAgentInterface:
    """Factory function to create the appropriate AI agent interface based on config."""
    logger = get_logger(__name__)
    
    if cfg.ai_agent.lower() == "claude":
        logger.info("Using Claude Code interface")
        return ClaudeCodeInterface(cfg)
    elif cfg.ai_agent.lower() == "opencode":
        logger.info("Using OpenCode CLI interface")
        return OpenCodeInterface(cfg)
    else:
        raise ValueError(f"Unknown AI agent: {cfg.ai_agent}. Must be 'claude' or 'opencode'")
