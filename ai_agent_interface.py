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
   
   **CRITICAL OUTPUT RULES:**
   - Use JIRA markup ONLY: h2., h3., *bold*, not markdown ##, **bold**
   - NO technical sections like "Current Implementation Issues" or "Implementation Notes"
   - NO code references, file names, line numbers, or technical details
   - Keep ALL output PM-friendly and business-focused
   
   **DO NOT** include:
   - "I'll analyze the codebase..."
   - "Let me search for..."
   - "Based on my analysis..."
   - Technical sections like "Current Implementation Issues"
   - Any technical analysis or code references
   - Any narration of what you're doing or thinking
   
   **JUST START** with the output format directly.

   a) **"CLARIFYING QUESTIONS:"** - If there are genuine functional/business gaps
      - Start with PM instructions explaining what to do
      - Then list questions that need clarification
      
      Format using clear plain text structure:
      ```
      Hi! I've analyzed the codebase and have some questions about the requirements that need clarification before I can implement this feature.
      
      → Please update the JIRA description with answers to these questions:
      
      ═══════════════════════════════════════════════════════
      CLARIFYING QUESTIONS
      ═══════════════════════════════════════════════════════
      
      1. [Question about business logic]
      
      2. [Question about edge case]
      ```
      
      Formatting rules:
      - Use ═══ lines and clear section headers for visual separation
      - Use numbered lists (1. 2. 3.) with blank lines between questions
      - Use → for emphasis instead of bold
      - Keep it clean and readable as plain text
      
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
      
      CRITICAL INSTRUCTIONS FOR ACCEPTANCE CRITERIA:
      - FIRST: Carefully read the JIRA description and identify ALL existing acceptance criteria
      - COMPARE: For each scenario you want to propose, check if it's ALREADY covered in the description
      - DO NOT output scenarios that are already in the description, even if worded slightly differently
      - ONLY output genuinely NEW edge cases or scenarios that are MISSING from the description
      - If the description already covers all major scenarios: respond with "READY TO IMPLEMENT" instead
      - Better to output NOTHING than to repeat what's already documented
      
      Format using clear plain text structure:
      ```
      Hi! I've analyzed the codebase and found some additional edge cases that aren't covered in the current acceptance criteria.
      
      → Please review these NEW scenarios and add them to the JIRA description if they're accurate. Once updated, I'll proceed with implementation.
      
      ═══════════════════════════════════════════════════════
      ADDITIONAL EDGE CASES TO CONSIDER
      ═══════════════════════════════════════════════════════
      
      GIVEN [NEW scenario not in description]
      WHEN [NEW condition not in description]
      THEN [NEW expected behavior not in description]
      ```
      
      Remember: DO NOT output scenarios already in the description!
      
      Formatting rules:
      - Use ═══ lines and clear section headers
      - Use GIVEN/WHEN/THEN in capitals for clarity
      - Use → for emphasis
      - Keep readable as plain text

   c) **"READY TO IMPLEMENT"** - If requirements are clear and no new scenarios needed
      - Use this if the description already has comprehensive acceptance criteria
      - Summarize understanding at BUSINESS level ONLY
      - NO technical details, NO code references, NO file names, NO implementation notes
      - DO NOT include "Current Implementation Issues" or similar technical sections
      - Keep it brief and PM-focused
      
      Format using clear plain text structure:
      ```
      Hi! I've analyzed the codebase and the requirements are clear. I understand:
      
      • [Key business point about the feature]
      • [Key business point about edge cases]
      
      → I'm ready to implement this feature. I'll create a feature branch and start coding.
      ```
      
      Example GOOD "READY TO IMPLEMENT":
      ```
      Hi! I've analyzed the codebase and the requirements are clear. I understand:
      
      • For OTA reservations, redirect guests to the OTA only for cancellations, date changes, and room changes
      • Handle other requests (services, preferences) directly for OTA reservations  
      • Process OTA notifications without redirecting
      
      → I'm ready to implement. I'll create a feature branch and start coding.
      ```
      
      Example BAD (DO NOT DO THIS):
      ```
      ## Current Implementation Issues
      
      1. Overly Broad Redirect Logic: The code in cancel_reservation_tool_abstract.py:97-98...
      ```

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

MANDATORY IMPLEMENTATION STEPS (in order):
1. Implement the feature according to requirements
2. Write tests (unit tests mandatory, integration tests if needed)
3. **RUN ALL TESTS LOCALLY** - This step is CRITICAL:
   - Backend tests: Run `pytest -n10` and ensure ALL tests pass
   - Frontend tests (if frontend changes): Run `npm run prepare-commit` and ensure it passes
   - DO NOT proceed if ANY test fails - fix the failures first
4. Only after ALL tests pass: Commit with message: "{issue_key}: <concise description>"
5. Push the branch to origin
6. Create a pull request against main branch with title "{issue_key}: <description>"

CRITICAL TEST REQUIREMENTS:
- You MUST run `pytest -n10` in the backend directory before claiming success
- If you made frontend changes, you MUST run `npm run prepare-commit` 
- DO NOT create a PR if tests fail locally
- DO NOT claim implementation is complete if tests fail
- If tests fail, analyze the failures, fix them, and re-run tests

Important:
- Follow existing code style exactly
- DO NOT create utility files like *_ACCEPTANCE_CRITERIA.md, *_PROGRESS.md, etc.
- You have full access to git operations and GitHub CLI/API

After completing the work AND TESTS PASS, respond with a status update formatted for JIRA:

If SUCCESSFUL (tests pass):
- Mention "Created PR #<number>" or include the PR URL
- State "All tests passed locally (pytest -n10 [and npm run prepare-commit if frontend])"
- List the key changes made
- Keep it concise and professional

If TESTS FAIL:
- State "Tests failed - implementation incomplete"
- List which tests failed and why
- Explain what needs to be fixed

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

MANDATORY STEPS:
1. Analyze the CI failure logs to understand what's failing
2. Identify and fix the root cause
3. Run tests locally to verify your fix (pytest -n10 for backend, npm run prepare-commit for frontend)
4. Only after tests pass locally: Commit with message "{issue_key}: Fix CI - <what was fixed>"
5. Push changes (will re-trigger CI automatically)

CRITICAL REQUIREMENTS:
- You MUST run tests locally before pushing (pytest -n10 and/or npm run prepare-commit)
- DO NOT push if tests still fail locally
- Fix the actual issue, don't mask it or skip tests
- DO NOT create utility files like *_NOTES.md, *_PROGRESS.md, etc.

Important:
- You're already on the correct branch
- The PR exists - just push your fixes
- You have full access to the codebase and can make any necessary changes

After fixing, respond with a status update using plain text formatting (NO markdown):

If FIXED (tests pass):
- State what was wrong and what you changed
- Mention "Tests now pass locally (pytest -n10 [and/or npm run prepare-commit])"
- Keep it clear and concise
- Use plain text (→ for emphasis, NOT markdown bold/italic)

If STUCK:
- Explain what you tried and why it didn't work
- State clearly what's blocking you
- Suggest what's needed to unblock

Your response will be posted as a JIRA comment. Use plain text formatting only.
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
            # Look for test failures first (highest priority)
            if any(keyword in output_lower for keyword in [
                'tests failed', 'test failed', 'pytest failed', 'test failure',
                'tests did not pass', 'tests are failing', 'failing tests',
                'implementation incomplete'
            ]):
                self.logger.info("Detected test failures - implementation incomplete")
                return {"action": "stuck", "message": output, "pr_number": None, "pr_url": None}
            
            # Look for PR creation (only valid if tests passed)
            if pr_match or pr_url_match or 'created pr' in output_lower or 'pull request' in output_lower:
                pr_number = int(pr_match.group(1)) if pr_match else None
                pr_url = pr_url_match.group(1) if pr_url_match else None
                
                # Check if output mentions tests passing
                tests_passed = any(phrase in output_lower for phrase in [
                    'tests passed', 'all tests pass', 'tests pass', 
                    'pytest -n10', 'npm run prepare-commit'
                ])
                
                if not tests_passed:
                    self.logger.warning(f"PR created but no mention of passing tests")
                
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
            
            # Convert Windows paths to forward slashes for cross-platform compatibility
            repo_path_unix = repo_path.replace('\\', '/')
            prompt_file_unix = prompt_file.replace('\\', '/')
            
            # Build OpenCode command with attachments, then pipe prompt via stdin
            opencode_cmd_parts = [
                f'"{self.opencode_cli_path}"',
                'run',
                f'"{repo_path_unix}"',
                '--model', 'opencode/minimax-m2.1-free'
            ]
            
            # Add attachments
            if attachment_paths:
                for attachment_path in attachment_paths:
                    attachment_path_unix = attachment_path.replace('\\', '/')
                    opencode_cmd_parts.extend(['-f', f'"{attachment_path_unix}"'])
                    self.logger.debug(f"Adding attachment: {attachment_path}")
            
            # Build PowerShell command that pipes prompt to opencode
            # Read prompt from file and pipe to opencode with all arguments
            opencode_cmd = ' '.join(opencode_cmd_parts)
            ps_cmd = f'Get-Content "{prompt_file_unix}" -Raw | & {opencode_cmd}'
            
            self.logger.debug(f"Running PowerShell command")
            
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',  # Replace invalid characters instead of failing
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
            
            if result.stdout is None:
                self.logger.error("OpenCode output is None - likely encoding issue")
                return {
                    "action": "error",
                    "message": "OpenCode CLI produced no output (encoding issue)",
                    "details": f"stderr: {result.stderr if result.stderr else 'No stderr'}"
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
