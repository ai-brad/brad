import re
import json
from typing import Dict, List, Optional
from pathlib import Path
from logging_config import get_logger
from coding_agent import CodingAgent
from codebase_map import get_codebase_map


class AIAgentInterface:
    """
    AI agent interface powered by CodingAgent (Azure OpenAI Responses API).
    Brad does everything directly — no external CLI tools needed.
    Supports warm-start: pass previous_response_id to continue conversation context.
    """
    
    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.cfg = cfg
        self.agent = CodingAgent(cfg)
    
    # ------------------------------------------------------------------
    # Public methods called by BradOrchestrator
    # ------------------------------------------------------------------
    def invoke_requirements_analysis(
        self,
        issue_key: str,
        description: str,
        attachment_paths: List[str],
        repo_path: str,
        iteration: int,
        previous_response_id: Optional[str] = None,
    ) -> Dict:
        self.logger.info(f"Requirements analysis: {issue_key} (iteration {iteration})")
        prompt = self._build_requirements_prompt(issue_key, description, attachment_paths, iteration)
        codebase_map = get_codebase_map(repo_path)
        output, response_id = self.agent.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
        result = self._parse_requirements_response(output)
        result["_response_id"] = response_id
        return result
    
    def invoke_implementation(
        self,
        issue_key: str,
        description: str,
        attachment_paths: List[str],
        repo_path: str,
        branch_name: str,
        iteration: int,
        previous_response_id: Optional[str] = None,
    ) -> Dict:
        self.logger.info(f"Implementation: {issue_key} (iteration {iteration})")
        prompt = self._build_implementation_prompt(issue_key, description, attachment_paths, branch_name, iteration)
        codebase_map = get_codebase_map(repo_path)
        output, response_id = self.agent.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
        result = self._parse_implementation_response(output)
        result["_response_id"] = response_id
        return result
    
    def invoke_review_fix(
        self,
        issue_key: str,
        description: str,
        review_comments: List[Dict],
        repo_path: str,
        branch_name: str,
        pr_number: int,
        iteration: int,
        previous_response_id: Optional[str] = None,
    ) -> Dict:
        self.logger.info(f"Review fix: {issue_key} PR#{pr_number} (iteration {iteration})")
        prompt = self._build_review_fix_prompt(issue_key, description, review_comments, pr_number, iteration)
        codebase_map = get_codebase_map(repo_path)
        output, response_id = self.agent.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
        result = self._parse_review_fix_response(output)
        result["_response_id"] = response_id
        return result
    
    def invoke_ci_fix(
        self,
        issue_key: str,
        description: str,
        ci_logs: str,
        failed_jobs: List[str],
        repo_path: str,
        branch_name: str,
        pr_number: int,
        iteration: int,
        failed_test_target: Optional[str] = None,
        previous_response_id: Optional[str] = None,
    ) -> Dict:
        self.logger.info(f"CI fix: {issue_key} PR#{pr_number} (iteration {iteration})")
        prompt = self._build_ci_fix_prompt(
            issue_key, description, ci_logs, failed_jobs, pr_number, iteration,
            failed_test_target=failed_test_target,
        )
        codebase_map = get_codebase_map(repo_path)
        output, response_id = self.agent.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
        result = self._parse_ci_fix_response(output)
        result["_response_id"] = response_id
        return result
    
    def invoke_local_review(
        self,
        issue_key: str,
        description: str,
        diff: str,
        repo_path: str,
        branch_name: str,
    ) -> Dict:
        """
        Invoke a FRESH agent session to review the implementation.
        Returns dict with action: "approved" | "changes_requested" and message.
        """
        self.logger.info(f"Local review: {issue_key} on branch {branch_name}")
        prompt = self._build_local_review_prompt(issue_key, description, diff, branch_name)
        output, response_id = self.agent.run(prompt, repo_path)
        return self._parse_local_review_response(output)
    
    
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
3. **RUN ONLY THE RELEVANT TESTS** — This step is CRITICAL:
   - The repo uses a .venv virtual environment — always use `.venv/Scripts/pytest.exe`, NOT bare pytest
   - Determine which test directories correspond to the source files you changed:
     src/bea/modules/<module>/... → tests/unit/modules/<module>/
     src/bea/base/...             → tests/unit/base/
     src/bea/common/...           → tests/unit/common/
   - Run ONLY those test directories: `.venv/Scripts/pytest.exe -x <test_dirs> -v`
   - Do NOT run the full test suite — that is what CI/CD is for
   - DO NOT proceed if targeted tests fail — fix the failures first
4. Only after tests pass: Commit with message: "{issue_key}: <concise description>"
5. Push the branch to origin: `git push origin {branch_name}`
6. Create a pull request: `gh pr create --repo {self.cfg.github_repo if hasattr(self, 'cfg') else 'flaerobotics/bea'} --base main --head {branch_name} --title "{issue_key}: <description>" --body "Automated PR for {issue_key}"`

CRITICAL TEST REQUIREMENTS:
- You MUST use .venv/Scripts/pytest.exe (NOT bare pytest) to run tests
- Run ONLY tests relevant to the files you changed — never the full suite
- DO NOT create a PR if tests fail locally
- DO NOT claim implementation is complete if tests fail
- If tests fail, analyze the failures, fix them, and re-run only the failing tests

Important:
- Follow existing code style exactly
- DO NOT create utility files like *_ACCEPTANCE_CRITERIA.md, *_PROGRESS.md, etc.
- You have full access to git operations and gh CLI for PRs

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
        iteration: int,
        failed_test_target: Optional[str] = None,
    ) -> str:
        """Build prompt for CI fix phase."""
        if failed_test_target:
            test_instruction = (
                f"3. Re-run ONLY the previously failing tests to verify your fix:\n"
                f"   `.venv/Scripts/pytest.exe -x {failed_test_target} -v`\n"
                f"   - Do NOT run the full test suite — CI will handle that\n"
                f"   - Use .venv/Scripts/pytest.exe, NOT bare pytest"
            )
        else:
            test_instruction = (
                "3. Re-run the relevant tests to verify your fix:\n"
                "   - Determine test dirs from the files you changed (src/bea/modules/<m>/... → tests/unit/modules/<m>/)\n"
                "   - `.venv/Scripts/pytest.exe -x <test_dirs> -v`\n"
                "   - Do NOT run the full test suite — CI will handle that\n"
                "   - Use .venv/Scripts/pytest.exe, NOT bare pytest"
            )
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
{test_instruction}
4. Only after tests pass locally: Commit with message "{issue_key}: Fix CI - <what was fixed>"
5. Push changes: `git push origin {issue_key}`

CRITICAL REQUIREMENTS:
- You MUST use .venv/Scripts/pytest.exe (NOT bare pytest) to run tests
- Run ONLY the previously failing tests — never the full suite
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
- Mention which tests now pass locally
- Keep it clear and concise
- Use plain text (→ for emphasis, NOT markdown bold/italic)

If STUCK:
- Explain what you tried and why it didn't work
- State clearly what's blocking you
- Suggest what's needed to unblock

Your response will be posted as a JIRA comment. Use plain text formatting only.
"""
    
    def _build_review_fix_prompt(
        self,
        issue_key: str,
        description: str,
        review_comments: List[Dict],
        pr_number: int,
        iteration: int
    ) -> str:
        """Build prompt for addressing PR review comments."""
        # Format review comments
        comments_text = ""
        for i, comment in enumerate(review_comments, 1):
            path = comment.get('path', 'N/A')
            line = comment.get('line', 'N/A')
            body = comment.get('body', '')
            user = comment.get('user', {}).get('login', 'Unknown')
            comments_text += f"\n{i}. File: {path}:{line}\n   Reviewer ({user}): {body}\n"
        
        return f"""You are Brad, addressing code review comments for JIRA issue {issue_key} (PR #{pr_number}).

Iteration: {iteration}

Original Requirements:
{description}

Code Review Comments:
{comments_text}

MANDATORY STEPS:
1. Read and understand each review comment carefully
2. Address each comment by making the requested changes to the code
3. Run ONLY the relevant tests:
   - Determine test dirs from the files you changed (src/bea/modules/<m>/... → tests/unit/modules/<m>/)
   - `.venv/Scripts/pytest.exe -x <test_dirs> -v`
   - The repo uses a .venv virtual environment — always use .venv/Scripts/pytest.exe, NOT bare pytest
   - Do NOT run the full test suite — CI will handle that
4. Only after tests pass: Commit with message "{issue_key}: Address review comments - <summary of changes>"
5. Push changes: `git push origin {issue_key}`

CRITICAL REQUIREMENTS:
- You MUST use .venv/Scripts/pytest.exe (NOT bare pytest) to run tests
- Run ONLY tests relevant to the files you changed — never the full suite
- DO NOT push if tests still fail locally
- Address ALL review comments, not just some of them
- Make sure your changes align with the reviewer's feedback
- DO NOT create utility files like *_NOTES.md, *_PROGRESS.md, etc.

Important:
- You're already on the correct branch
- The PR exists - just push your fixes
- You have full access to the codebase and can make any necessary changes

After addressing the comments, respond with a status update using plain text formatting (NO markdown):

If FIXED (tests pass):
- Summarize what changes you made in response to the review
- Mention "Tests still pass locally (pytest -n10 [and/or npm run prepare-commit])"
- Keep it clear and concise
- Use plain text (→ for emphasis, NOT markdown bold/italic)

If STUCK:
- Explain what review comments you couldn't address and why
- State clearly what's blocking you
- Suggest what's needed to unblock

Your response will be posted as a JIRA comment. Use plain text formatting only.
"""


    # ------------------------------------------------------------------
    # Response parsers
    # ------------------------------------------------------------------
    def _parse_requirements_response(self, output: str) -> Dict:
        output_lower = output.lower().strip()
        if output.startswith("ERROR:"):
            return {"action": "error", "message": output, "details": output}
        # Ready takes priority — if requirements are clear, go straight to implementation
        if any(kw in output_lower for kw in ['ready to implement', 'requirements are clear', 'can proceed']):
            self.logger.info("Detected: ready to implement")
            return {"action": "ready", "message": output, "details": ""}
        # Only detect clarify if output explicitly starts with the expected format
        if output_lower.startswith('clarifying questions'):
            self.logger.info("Detected: clarification needed")
            return {"action": "clarify", "message": output, "details": ""}
        if any(kw in output_lower for kw in ['acceptance criteria', 'given', 'when', 'then']):
            self.logger.info("Detected: acceptance criteria")
            return {"action": "propose_scenarios", "message": output, "details": ""}
        # Default to ready — bias toward action
        self.logger.info("Defaulting to: ready to implement")
        return {"action": "ready", "message": output, "details": ""}

    def _parse_implementation_response(self, output: str) -> Dict:
        output_lower = output.lower()
        if output.startswith("ERROR:"):
            return {"action": "error", "message": output, "pr_number": None, "pr_url": None}

        # Check for test failures first
        if any(kw in output_lower for kw in ['tests failed', 'test failed', 'implementation incomplete']):
            self.logger.info("Detected: tests failed")
            return {"action": "stuck", "message": output, "pr_number": None, "pr_url": None}

        # Extract PR info
        pr_match = re.search(r'pr\s*#?(\d+)', output_lower)
        pr_url_match = re.search(r'(https://github\.com/[^\s]+/pull/\d+)', output, re.IGNORECASE)
        if pr_match or pr_url_match or 'created pr' in output_lower or 'pull request' in output_lower:
            pr_number = int(pr_match.group(1)) if pr_match else None
            pr_url = pr_url_match.group(1) if pr_url_match else None
            self.logger.info(f"Detected: PR #{pr_number} at {pr_url}")
            return {"action": "success", "pr_number": pr_number, "pr_url": pr_url, "message": output}

        if any(kw in output_lower for kw in ['stuck', 'cannot', 'unable to', 'blocked']):
            self.logger.info("Detected: stuck/blocked")
            return {"action": "stuck", "message": output, "pr_number": None, "pr_url": None}

        self.logger.info("Detected: in progress (no PR yet)")
        return {"action": "in_progress", "message": output, "pr_number": None, "pr_url": None}

    def _parse_ci_fix_response(self, output: str) -> Dict:
        output_lower = output.lower()
        if output.startswith("ERROR:"):
            return {"action": "error", "message": output}
        if any(kw in output_lower for kw in ['fixed', 'resolved', 'tests passing', 'should pass now']):
            self.logger.info("Detected: CI fix completed")
            return {"action": "fixed", "message": output}
        self.logger.info("Detected: stuck on CI fix")
        return {"action": "stuck", "message": output}

    def _parse_review_fix_response(self, output: str) -> Dict:
        output_lower = output.lower()
        if output.startswith("ERROR:"):
            return {"action": "error", "message": output}
        if any(kw in output_lower for kw in ['addressed', 'fixed', 'resolved', 'updated', 'tests pass']):
            self.logger.info("Detected: review fix completed")
            return {"action": "fixed", "message": output}
        self.logger.info("Detected: stuck on review fix")
        return {"action": "stuck", "message": output}

    def _parse_local_review_response(self, output: str) -> Dict:
        output_lower = output.lower()
        if output.startswith("ERROR:"):
            return {"action": "error", "message": output}
        if any(kw in output_lower for kw in ['approved', 'lgtm', 'looks good', 'no issues']):
            self.logger.info("Local review: APPROVED")
            return {"action": "approved", "message": output}
        self.logger.info("Local review: changes requested")
        return {"action": "changes_requested", "message": output}

    # ------------------------------------------------------------------
    # Local review prompt
    # ------------------------------------------------------------------
    def _build_local_review_prompt(
        self,
        issue_key: str,
        description: str,
        diff: str,
        branch_name: str,
    ) -> str:
        return f"""You are a senior code reviewer. Review the following changes for JIRA issue {issue_key}.

Branch: {branch_name}

Requirements:
{description}

Git diff of all changes:
{diff}

REVIEW INSTRUCTIONS:
1. Use the tools to explore the codebase and understand context around the changes
2. Check that the implementation matches the requirements
3. Look for bugs, edge cases, missing error handling
4. Check code style consistency with the rest of the codebase
5. Verify test coverage

RESPOND with ONE of:
a) "APPROVED" - if the code is correct and ready to merge
   - Briefly explain why it looks good
   
b) "CHANGES REQUESTED:" - if there are issues
   - List each issue with file path and description
   - Be specific about what needs to change
   - Focus on real bugs and issues, not nitpicks

Keep your review concise and actionable. This will be posted as a JIRA comment.
"""


def create_ai_agent_interface(cfg) -> AIAgentInterface:
    """Create the AI agent interface. Single implementation — CodingAgent."""
    logger = get_logger(__name__)
    logger.info("Creating DirectAgent (Azure OpenAI Responses API)")
    return AIAgentInterface(cfg)
