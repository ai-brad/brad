"""
AI Agent Interface — prompt building and response parsing for all Brad phases.

This module bridges the Orchestrator and the LLM adapter. It builds structured
prompts for each phase (requirements, implementation, CI fix, review fix, local
review) and parses the LLM output into action dicts.
"""

import re
import subprocess
from typing import Dict, List, Optional
from pathlib import Path
from brad.logging_config import get_logger
from brad.adapters.llm.base import LLMAdapter
from brad.codebase_map import get_codebase_map


class AIAgentInterface:
    """
    AI agent interface powered by an LLM adapter.
    Supports warm-start: pass previous_response_id to continue conversation context.
    """

    def __init__(self, llm: LLMAdapter, cfg):
        self.logger = get_logger(__name__)
        self.cfg = cfg
        self.llm = llm

    # ------------------------------------------------------------------
    # Public methods called by Orchestrator
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
        result = self.llm.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
        parsed = self._parse_requirements_response(result.text)
        parsed["_response_id"] = result.response_id
        parsed["_usage"] = result.usage
        return parsed

    def invoke_implementation(
        self,
        issue_key: str,
        description: str,
        attachment_paths: List[str],
        repo_path: str,
        branch_name: str,
        iteration: int,
        previous_response_id: Optional[str] = None,
        dev_instructions: str = "",
    ) -> Dict:
        self.logger.info(f"Implementation: {issue_key} (iteration {iteration})")
        pre_search = self._pre_search_codebase(description, repo_path)
        prompt = self._build_implementation_prompt(issue_key, description, attachment_paths, branch_name, iteration, pre_search, dev_instructions)
        codebase_map = get_codebase_map(repo_path)
        result = self.llm.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
        parsed = self._parse_implementation_response(result.text)
        parsed["_response_id"] = result.response_id
        parsed["_usage"] = result.usage
        return parsed

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
        dev_instructions: str = "",
    ) -> Dict:
        self.logger.info(f"Review fix: {issue_key} PR#{pr_number} (iteration {iteration})")
        prompt = self._build_review_fix_prompt(issue_key, description, review_comments, pr_number, iteration, dev_instructions)
        codebase_map = get_codebase_map(repo_path)
        result = self.llm.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
        parsed = self._parse_review_fix_response(result.text)
        parsed["_response_id"] = result.response_id
        parsed["_usage"] = result.usage
        return parsed

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
        dev_instructions: str = "",
    ) -> Dict:
        self.logger.info(f"CI fix: {issue_key} PR#{pr_number} (iteration {iteration})")
        prompt = self._build_ci_fix_prompt(
            issue_key, description, ci_logs, failed_jobs, pr_number, iteration,
            failed_test_target=failed_test_target, dev_instructions=dev_instructions,
        )
        codebase_map = get_codebase_map(repo_path)
        result = self.llm.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
        parsed = self._parse_ci_fix_response(result.text)
        parsed["_response_id"] = result.response_id
        parsed["_usage"] = result.usage
        return parsed

    def invoke_local_review(
        self,
        issue_key: str,
        description: str,
        diff: str,
        repo_path: str,
        branch_name: str,
    ) -> Dict:
        self.logger.info(f"Local review: {issue_key} on branch {branch_name}")
        prompt = self._build_local_review_prompt(issue_key, description, diff, branch_name)
        result = self.llm.run(prompt, repo_path)
        parsed = self._parse_local_review_response(result.text)
        parsed["_usage"] = result.usage
        return parsed

    def invoke_local_review_fix(
        self,
        issue_key: str,
        description: str,
        review_feedback: str,
        repo_path: str,
        branch_name: str,
        iteration: int,
        previous_response_id: Optional[str] = None,
        dev_instructions: str = "",
    ) -> Dict:
        self.logger.info(f"Local review fix: {issue_key} on branch {branch_name} (iteration {iteration})")
        prompt = self._build_local_review_fix_prompt(issue_key, description, review_feedback, branch_name, iteration, dev_instructions)
        codebase_map = get_codebase_map(repo_path)
        result = self.llm.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
        parsed = self._parse_local_review_fix_response(result.text)
        parsed["_response_id"] = result.response_id
        parsed["_usage"] = result.usage
        return parsed

    def invoke_code_review_reader(
        self,
        pr_number: int,
        branch_name: str,
        comment: Dict,
        repo_path: str,
        previous_response_id: Optional[str] = None,
    ) -> Dict:
        """Analyze a single PR review comment for legitimacy and decide action."""
        self.logger.info(f"Code review reader: PR #{pr_number}, comment by {comment.get('user', {}).get('login', 'unknown')}")
        prompt = self._build_code_review_reader_prompt(pr_number, branch_name, comment)
        codebase_map = get_codebase_map(repo_path)
        result = self.llm.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
        parsed = self._parse_code_review_reader_response(result.text)
        parsed["_response_id"] = result.response_id
        parsed["_usage"] = result.usage
        return parsed

    def invoke_code_review_reader_batch(
        self,
        pr_number: int,
        branch_name: str,
        comments: List[Dict],
        repo_path: str,
        previous_response_id: Optional[str] = None,
        dev_instructions: str = "",
    ) -> Dict:
        """Analyze ALL review comments for a PR in a single LLM call."""
        self.logger.info(f"Code review reader (batch): PR #{pr_number}, {len(comments)} comments")
        prompt = self._build_code_review_reader_batch_prompt(pr_number, branch_name, comments, dev_instructions)
        codebase_map = get_codebase_map(repo_path)
        result = self.llm.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
        parsed = self._parse_code_review_reader_batch_response(result.text, comments)
        parsed["_response_id"] = result.response_id
        parsed["_usage"] = result.usage
        return parsed

    # ------------------------------------------------------------------
    # Pre-search helper
    # ------------------------------------------------------------------
    def _pre_search_codebase(self, description: str, repo_path: str) -> str:
        """Extract key terms from the ticket and run git grep to find relevant files."""
        # Extract meaningful terms: CamelCase identifiers, UPPER_CASE constants, quoted strings
        terms = set()
        # CamelCase / PascalCase identifiers (at least 2 words)
        for m in re.finditer(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', description):
            terms.add(m.group(1))
        # UPPER_CASE_CONSTANTS
        for m in re.finditer(r'\b([A-Z][A-Z0-9_]{2,})\b', description):
            terms.add(m.group(1))
        # Quoted strings (likely identifiers or tag names)
        for m in re.finditer(r'["\']([a-zA-Z_][a-zA-Z0-9_.]{2,})["\']', description):
            terms.add(m.group(1))
        # snake_case identifiers that look like code
        for m in re.finditer(r'\b([a-z][a-z0-9]*_[a-z0-9_]+)\b', description):
            if len(m.group(1)) > 4:
                terms.add(m.group(1))

        if not terms:
            return ""

        # Limit to most specific terms (longest first, max 8)
        sorted_terms = sorted(terms, key=len, reverse=True)[:8]
        self.logger.info(f"Pre-search terms: {sorted_terms}")

        results = []
        seen_files = set()
        for term in sorted_terms:
            try:
                result = subprocess.run(
                    ["git", "grep", "-l", "-i", term, "--", "*.py"],
                    capture_output=True, text=True, timeout=10,
                    cwd=repo_path, encoding="utf-8", errors="replace",
                )
                if result.returncode == 0 and result.stdout.strip():
                    files = result.stdout.strip().splitlines()
                    new_files = [f for f in files if f not in seen_files][:5]
                    if new_files:
                        results.append(f"  '{term}' found in: {', '.join(new_files)}")
                        seen_files.update(new_files)
            except Exception:
                continue

        if not results:
            return ""

        output = "\n".join(results)
        if len(output) > 4000:
            output = output[:4000] + "\n... [truncated]"
        self.logger.info(f"Pre-search found {len(seen_files)} relevant files")
        return output

    # ------------------------------------------------------------------
    # Prompt builders
    # ------------------------------------------------------------------
    def _build_requirements_prompt(self, issue_key, description, attachment_paths, iteration):
        attachments_text = ""
        if attachment_paths:
            attachments_text = "\n\nAttachments available:\n" + "\n".join(
                f"- {Path(p).name}: {p}" for p in attachment_paths
            )

        return f"""You are Brad, an autonomous senior software engineer analyzing issue {issue_key}.

Issue Description:
{description}
{attachments_text}

CRITICAL INSTRUCTIONS:
1. Search and analyze the codebase thoroughly to understand current behavior
2. Translate findings into BUSINESS/FUNCTIONAL language for the PM
3. RESPOND with ONE of these formats:
   - "CLARIFYING QUESTIONS:" followed by questions
   - "ACCEPTANCE CRITERIA:" followed by scenarios
   - "READY TO IMPLEMENT" followed by summary

Use plain text formatting. Keep output PM-friendly and business-focused.
No code references, file names, or technical jargon.

If requirements are clear and acceptance criteria already exist: respond with "READY TO IMPLEMENT".
"""

    def _build_implementation_prompt(self, issue_key, description, attachment_paths, branch_name, iteration, pre_search="", dev_instructions=""):
        attachments_text = ""
        if attachment_paths:
            attachments_text = "\n\nAttachments:\n" + "\n".join(
                f"- {Path(p).name}: {p}" for p in attachment_paths
            )

        pre_search_text = ""
        if pre_search:
            pre_search_text = f"\n\nPRE-SEARCH RESULTS (relevant files found by searching the codebase for key terms from the requirements — use these to skip exploration and start implementing faster):\n{pre_search}\n"

        return f"""You are Brad, an autonomous software engineer implementing issue {issue_key}.

Branch: {branch_name}
Iteration: {iteration}

Requirements:
{description}
{attachments_text}
{pre_search_text}
MANDATORY IMPLEMENTATION STEPS (in order):
1. Implement the feature according to requirements
2. Write tests appropriate for the change type:
   - **Backend logic**: unit tests in tests/unit/ (mandatory)
   - **API endpoints**: integration tests exercising the endpoint
   - **UI/frontend templates**: template rendering tests (verify HTML output contains expected elements)
   - **Database changes**: migration tests and data integrity tests
   - **Configuration**: config validation tests
3. Run ONLY the relevant tests locally to verify — try to fix failures but do NOT get stuck on environment issues
4. Commit ALL changes with message: "{issue_key}: <concise description>"
5. Push the branch to origin: `git push origin {branch_name}`
6. Create a pull request against main using: `gh pr create --base main --head {branch_name} --title "Brad: {issue_key}: <summary>" --body "<description>"`

CRITICAL EFFICIENCY RULES (cost budget is limited):
- Use the pre-search results above to go directly to the relevant files — do NOT do broad exploratory searches
- Use `multi_edit_file` to batch ALL edits to multiple files in a single call instead of editing files one by one
- Follow existing code style exactly
- DO NOT create utility files like *_ACCEPTANCE_CRITERIA.md, *_PROGRESS.md, etc.
- On Windows, set environment variables with `set VAR=value && command` (not Unix VAR=value syntax)
- ALWAYS commit, push, and create PR even if local tests have minor issues — the CI pipeline is the real test authority
- MINIMIZE iterations: read files in batches, edit files in batches, think before acting
- DO NOT re-read files you have already read unless they changed
{f"""
REPOSITORY DEV INSTRUCTIONS (follow these when running commands, tests, etc.):
{dev_instructions}
""" if dev_instructions else ""}
After completing the work, respond with a status update that MUST include the PR URL:
- If PR CREATED: include the PR number and URL, e.g. "Created PR #123: https://github.com/.../pull/123"
- If STUCK and unable to create PR: clearly state what blocked you
"""

    def _build_ci_fix_prompt(self, issue_key, description, ci_logs, failed_jobs, pr_number, iteration, failed_test_target=None, dev_instructions=""):
        if failed_test_target:
            test_instruction = (
                f"3. Re-run ONLY the previously failing tests to verify your fix:\n"
                f"   Run: {failed_test_target}\n"
                f"   Do NOT run the full test suite — CI will handle that"
            )
        else:
            test_instruction = (
                "3. Re-run relevant tests to verify your fix\n"
                "   Do NOT run the full test suite — CI will handle that"
            )
        return f"""You are Brad, fixing CI failures for issue {issue_key} (PR #{pr_number}).

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

After fixing, respond with status. If FIXED: state what was wrong and changed. If STUCK: explain why.
{f"""
REPOSITORY DEV INSTRUCTIONS (follow these when running commands, tests, etc.):
{dev_instructions}
""" if dev_instructions else ""}"""

    def _build_review_fix_prompt(self, issue_key, description, review_comments, pr_number, iteration, dev_instructions=""):
        comments_text = ""
        for i, comment in enumerate(review_comments, 1):
            path = comment.get('path', 'N/A')
            line = comment.get('line', 'N/A')
            body = comment.get('body', '')
            user = comment.get('user', {}).get('login', 'Unknown')
            comments_text += f"\n{i}. File: {path}:{line}\n   Reviewer ({user}): {body}\n"

        return f"""You are Brad, addressing code review comments for issue {issue_key} (PR #{pr_number}).

Iteration: {iteration}

Original Requirements:
{description}

Code Review Comments:
{comments_text}

MANDATORY STEPS:
1. Read and understand each review comment carefully
2. Address each comment by making the requested changes
3. Run relevant tests to ensure nothing is broken
4. Only after tests pass: Commit with message "{issue_key}: Address review comments - <summary>"
5. Push changes: `git push origin {issue_key}`

After addressing comments, respond with status. Address ALL review comments, not just some.
{f"""
REPOSITORY DEV INSTRUCTIONS (follow these when running commands, tests, etc.):
{dev_instructions}
""" if dev_instructions else ""}"""

    def _build_code_review_reader_prompt(self, pr_number, branch_name, comment):
        file_path = comment.get('path', 'N/A')
        line = comment.get('line', comment.get('original_line', 'N/A'))
        body = comment.get('body', '')
        reviewer = comment.get('user', {}).get('login', 'unknown')
        diff_hunk = comment.get('diff_hunk', '')

        return f"""You are Brad, an autonomous software engineer analyzing a code review comment on PR #{pr_number}.

Branch: {branch_name}

Review Comment:
- Reviewer: {reviewer}
- File: {file_path}:{line}
- Comment: {body}

Diff context:
{diff_hunk}

ANALYSIS INSTRUCTIONS:
1. Read the comment carefully and understand what the reviewer is asking
2. Examine the file and surrounding code to understand the full context
3. Determine the comment's category:
   a) LEGITIMATE BUG/ISSUE - the reviewer found a real problem that needs a code fix
   b) VALID SUGGESTION - the reviewer suggests an improvement worth making
   c) STYLE/NITPICK - low-value formatting or naming preference; can be acknowledged but not necessarily changed
   d) QUESTION - the reviewer is asking for clarification, not requesting a change
   e) INCORRECT/INVALID - the reviewer misunderstood the code; no change needed

ACTION RULES:
- For (a) and (b): Fix the code, run relevant tests, commit and push. Then reply explaining what you changed.
- For (c): If trivial, make the change. If opinionated, reply politely explaining your reasoning and resolve.
- For (d): Reply with a clear explanation.
- For (e): Reply politely explaining why the current code is correct.

After completing your action, respond with ONE of these:
- "CODE_CHANGED: <summary of what was changed>" — if you modified code
- "REPLIED: <your reply text>" — if you only replied without code changes
- "STUCK: <reason>" — if you cannot resolve this
"""

    def _build_code_review_reader_batch_prompt(self, pr_number, branch_name, comments, dev_instructions=""):
        comments_section = ""
        for i, comment in enumerate(comments, 1):
            file_path = comment.get('path', 'N/A')
            line = comment.get('line', comment.get('original_line', 'N/A'))
            body = comment.get('body', '')
            reviewer = comment.get('user', {}).get('login', 'unknown')
            diff_hunk = comment.get('diff_hunk', '')
            comment_id = comment.get('id', 'unknown')
            comments_section += f"\n--- COMMENT {i} (id={comment_id}) ---\n"
            comments_section += f"Reviewer: {reviewer}\n"
            comments_section += f"File: {file_path}:{line}\n"
            comments_section += f"Comment: {body}\n"
            comments_section += f"Diff context:\n{diff_hunk}\n"

        return f"""You are Brad, an autonomous software engineer analyzing {len(comments)} code review comments on PR #{pr_number}.

Branch: {branch_name}

Review Comments:
{comments_section}

ANALYSIS INSTRUCTIONS:
1. Read ALL comments carefully
2. Examine each referenced file to understand the full context
3. For each comment, determine its category:
   a) LEGITIMATE BUG/ISSUE - needs a code fix
   b) VALID SUGGESTION - worth making
   c) STYLE/NITPICK - acknowledge but not necessarily change
   d) QUESTION - reply with explanation
   e) INCORRECT/INVALID - explain why current code is correct
   f) TEST/VERIFICATION REQUEST - reviewer asks to run tests or provide evidence

ACTION RULES:
- For (a) and (b): Fix the code. Batch all fixes before running tests.
- For (c): If trivial, make the change. If opinionated, just reply.
- For (d) and (e): Reply only.
- For (f): Actually RUN the requested tests/commands and paste the output as your reply. Do not just say you cannot — use the run_command tool.
- Run tests ONCE after all code changes, not after each individual fix.
- Commit all changes in a single commit.

RESPONSE FORMAT — you MUST respond with a result for EACH comment using this exact format:

COMMENT <id>: CODE_CHANGED: <summary>
or
COMMENT <id>: REPLIED: <reply text>
or
COMMENT <id>: STUCK: <reason>

List ALL {len(comments)} comments in your response, one per line.
{f"""
REPOSITORY DEV INSTRUCTIONS (follow these when running commands, tests, etc.):
{dev_instructions}
""" if dev_instructions else ""}"""

    def _build_local_review_fix_prompt(self, issue_key, description, review_feedback, branch_name, iteration, dev_instructions=""):
        return f"""You are Brad, addressing local code review feedback for issue {issue_key}.

Branch: {branch_name}
Iteration: {iteration}

Original Requirements:
{description}

Local Review Feedback:
{review_feedback}

MANDATORY STEPS:
1. Read and understand the review feedback carefully
2. Address the requested changes in code
3. Run relevant tests to ensure nothing is broken
4. Only after tests pass: Commit with message "{issue_key}: Address local review feedback - <summary>"
5. Push changes: `git push origin {branch_name}`

After addressing the feedback, respond with ONE of these:
- "FIXED: <summary of what was changed>" — if you updated the code
- "STUCK: <reason>" — if you cannot resolve the feedback
{f"""
REPOSITORY DEV INSTRUCTIONS (follow these when running commands, tests, etc.):
{dev_instructions}
""" if dev_instructions else ""}"""

    def _build_local_review_prompt(self, issue_key, description, diff, branch_name):
        return f"""You are a senior code reviewer. Review the following changes for issue {issue_key}.

Branch: {branch_name}

Requirements:
{description}

Git diff of all changes:
{diff}

REVIEW INSTRUCTIONS:
1. Use tools to explore context around the changes
2. Check implementation matches requirements
3. Look for bugs, edge cases, missing error handling
4. Check code style consistency
5. Verify test coverage

RESPOND with ONE of:
a) "APPROVED" - if the code is correct and ready to merge
b) "CHANGES REQUESTED:" - if there are issues (list each with file path)
"""

    # ------------------------------------------------------------------
    # Response parsers
    # ------------------------------------------------------------------
    def _parse_requirements_response(self, output: str) -> Dict:
        output_lower = output.lower().strip()
        if output.startswith("ERROR:"):
            return {"action": "error", "message": output, "details": output}
        if any(kw in output_lower for kw in ['ready to implement', 'requirements are clear', 'can proceed']):
            self.logger.info("Detected: ready to implement")
            return {"action": "ready", "message": output, "details": ""}
        if output_lower.startswith('clarifying questions'):
            self.logger.info("Detected: clarification needed")
            return {"action": "clarify", "message": output, "details": ""}
        if any(kw in output_lower for kw in ['acceptance criteria', 'given', 'when', 'then']):
            self.logger.info("Detected: acceptance criteria")
            return {"action": "propose_scenarios", "message": output, "details": ""}
        self.logger.info("Defaulting to: ready to implement")
        return {"action": "ready", "message": output, "details": ""}

    def _parse_implementation_response(self, output: str) -> Dict:
        output_lower = output.lower()
        if output.startswith("ERROR:"):
            return {"action": "error", "message": output, "pr_number": None, "pr_url": None}

        # Check for PR creation FIRST — this takes priority over everything else
        pr_match = re.search(r'pr\s*#?(\d+)', output_lower)
        pr_url_match = re.search(r'(https://github\.com/[^\s]+/pull/\d+)', output, re.IGNORECASE)
        if pr_match or pr_url_match or 'created pr' in output_lower or 'pull request' in output_lower:
            pr_number = int(pr_match.group(1)) if pr_match else None
            pr_url = pr_url_match.group(1) if pr_url_match else None
            self.logger.info(f"Detected: PR #{pr_number} at {pr_url}")
            return {"action": "success", "pr_number": pr_number, "pr_url": pr_url, "message": output}

        # Only if no PR was detected, check for failure/stuck keywords
        if any(kw in output_lower for kw in ['tests failed', 'test failed', 'implementation incomplete']):
            self.logger.info("Detected: tests failed (no PR created)")
            return {"action": "stuck", "message": output, "pr_number": None, "pr_url": None}
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

    def _parse_local_review_fix_response(self, output: str) -> Dict:
        output_lower = output.lower()
        if output.startswith("ERROR:"):
            return {"action": "error", "message": output}
        if any(kw in output_lower for kw in ['fixed', 'addressed', 'resolved', 'updated', 'tests pass']):
            self.logger.info("Detected: local review fix completed")
            return {"action": "fixed", "message": output}
        self.logger.info("Detected: stuck on local review fix")
        return {"action": "stuck", "message": output}

    def _parse_code_review_reader_response(self, output: str) -> Dict:
        output_lower = output.lower()
        if output.startswith("ERROR:"):
            return {"action": "error", "message": output, "reply": ""}
        if 'code_changed' in output_lower:
            self.logger.info("Code review reader: code changed")
            return {"action": "code_changed", "message": output, "reply": output.split(":", 1)[-1].strip()}
        if 'replied' in output_lower:
            self.logger.info("Code review reader: replied only")
            reply_text = output.split(":", 1)[-1].strip() if ":" in output else output
            return {"action": "replied", "message": output, "reply": reply_text}
        if 'stuck' in output_lower:
            self.logger.info("Code review reader: stuck")
            return {"action": "stuck", "message": output, "reply": ""}
        self.logger.info("Code review reader: defaulting to replied")
        return {"action": "replied", "message": output, "reply": output[:500]}

    def _parse_code_review_reader_batch_response(self, output: str, comments: List[Dict]) -> Dict:
        """Parse batch review response. Extract per-comment results."""
        comment_results = []
        comment_ids = [c.get('id') for c in comments]

        for comment in comments:
            cid = comment.get('id')
            # Try to find a line matching "COMMENT <id>: <ACTION>: <text>"
            pattern = re.compile(
                rf'COMMENT\s+{re.escape(str(cid))}\s*:\s*(CODE_CHANGED|REPLIED|STUCK)\s*:\s*(.*)',
                re.IGNORECASE
            )
            match = pattern.search(output)
            if match:
                action_str = match.group(1).lower()
                reply_text = match.group(2).strip()
                comment_results.append({
                    'comment_id': cid,
                    'action': action_str,
                    'reply': reply_text,
                })
            else:
                # Fallback: try to infer from overall output
                self.logger.warning(f"Could not parse result for comment {cid}, defaulting to replied")
                comment_results.append({
                    'comment_id': cid,
                    'action': 'replied',
                    'reply': f"Brad reviewed this comment. See latest changes on the PR.",
                })

        self.logger.info(f"Code review reader batch: {len(comment_results)} results parsed")
        return {'comment_results': comment_results, 'message': output}

    def _parse_local_review_response(self, output: str) -> Dict:
        output_lower = output.lower()
        if output.startswith("ERROR:"):
            return {"action": "error", "message": output}
        if any(kw in output_lower for kw in ['approved', 'lgtm', 'looks good', 'no issues']):
            self.logger.info("Local review: APPROVED")
            return {"action": "approved", "message": output}
        self.logger.info("Local review: changes requested")
        return {"action": "changes_requested", "message": output}
