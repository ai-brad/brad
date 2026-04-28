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
from brad.adapters.harness import AgentHarness
from brad.codebase_map import get_codebase_map


class AIAgentInterface:
    """
    AI agent interface powered by a swappable :class:`AgentHarness`.
    Supports warm-start: pass previous_response_id to continue conversation context.
    """

    _PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
    MAX_PR_DIFF_LENGTH = 8000  # Characters - truncate to avoid exceeding context limits

    def __init__(self, harness: AgentHarness, cfg):
        self.logger = get_logger(__name__)
        self.cfg = cfg
        self.harness = harness
        self._prompt_cache: Dict[str, str] = {}

    def _load_prompt(self, name: str) -> str:
        """Load a prompt template from the prompts/ directory, with caching."""
        if name in self._prompt_cache:
            return self._prompt_cache[name]
        prompt_path = self._PROMPTS_DIR / name
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
        content = prompt_path.read_text(encoding="utf-8")
        self._prompt_cache[name] = content
        self.logger.debug(f"Loaded prompt template: {name} ({len(content)} chars)")
        return content

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
        result = self.harness.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
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
        result = self.harness.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
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
        result = self.harness.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
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
        result = self.harness.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
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
        result = self.harness.run(prompt, repo_path)
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
        result = self.harness.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
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
        result = self.harness.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
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
        pr_diff: str = "",
    ) -> Dict:
        """Analyze ALL review comments for a PR in a single LLM call."""
        self.logger.info(f"Code review reader (batch): PR #{pr_number}, {len(comments)} comments")
        prompt = self._build_code_review_reader_batch_prompt(pr_number, branch_name, comments, dev_instructions, pr_diff=pr_diff)
        codebase_map = get_codebase_map(repo_path)
        result = self.harness.run(prompt, repo_path, system_prompt=codebase_map, previous_response_id=previous_response_id)
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

        template = self._load_prompt("requirements.txt")
        return template.format(
            issue_key=issue_key,
            description=description,
            attachments_text=attachments_text,
        )

    def _build_implementation_prompt(self, issue_key, description, attachment_paths, branch_name, iteration, pre_search="", dev_instructions=""):
        attachments_text = ""
        if attachment_paths:
            attachments_text = "\n\nAttachments:\n" + "\n".join(
                f"- {Path(p).name}: {p}" for p in attachment_paths
            )

        pre_search_text = ""
        if pre_search:
            pre_search_text = f"\n\nPRE-SEARCH RESULTS (relevant files found by searching the codebase for key terms from the requirements — use these to skip exploration and start implementing faster):\n{pre_search}\n"

        dev_instructions_section = f"\nREPOSITORY DEV INSTRUCTIONS (follow these when running commands, tests, etc.):\n{dev_instructions}\n" if dev_instructions else ""

        template = self._load_prompt("implementation.txt")
        return template.format(
            issue_key=issue_key,
            branch_name=branch_name,
            iteration=iteration,
            description=description,
            attachments_text=attachments_text,
            pre_search_text=pre_search_text,
            dev_instructions_section=dev_instructions_section,
        )

    def _build_ci_fix_prompt(self, issue_key, description, ci_logs, failed_jobs, pr_number, iteration, failed_test_target=None, dev_instructions=""):
        if failed_test_target:
            test_instruction = (
                f"3. Re-run ONLY the previously failing tests to verify your fix:\n"
                f"   Run: {failed_test_target}\n"
                f"   Do NOT run the full test suite — CI will handle that"
            )
        else:
            test_instruction = (
                "3. Re-run relevant tests to verify your fix (consult dev instructions for commands):\n"
                "   - If CI failed on backend/Python tests → run backend unit tests (e.g., pytest)\n"
                "   - If CI failed on frontend/lint/UI tests → run frontend lint + tests (e.g., npm run prepare-commit)\n"
                "   Do NOT run the full test suite — CI will handle that"
            )

        dev_instructions_section = f"\nREPOSITORY DEV INSTRUCTIONS (follow these when running commands, tests, etc.):\n{dev_instructions}\n" if dev_instructions else ""

        template = self._load_prompt("ci_fix.txt")
        return template.format(
            issue_key=issue_key,
            pr_number=pr_number,
            iteration=iteration,
            description=description,
            failed_jobs=', '.join(failed_jobs),
            ci_logs=ci_logs,
            test_instruction=test_instruction,
            dev_instructions_section=dev_instructions_section,
        )

    def _build_review_fix_prompt(self, issue_key, description, review_comments, pr_number, iteration, dev_instructions=""):
        comments_text = ""
        for i, comment in enumerate(review_comments, 1):
            path = comment.get('path', 'N/A')
            line = comment.get('line', 'N/A')
            body = comment.get('body', '')
            user = comment.get('user', {}).get('login', 'Unknown')
            comments_text += f"\n{i}. File: {path}:{line}\n   Reviewer ({user}): {body}\n"

        dev_instructions_section = f"\nREPOSITORY DEV INSTRUCTIONS (follow these when running commands, tests, etc.):\n{dev_instructions}\n" if dev_instructions else ""

        template = self._load_prompt("review_fix.txt")
        return template.format(
            issue_key=issue_key,
            pr_number=pr_number,
            iteration=iteration,
            description=description,
            comments_text=comments_text,
            dev_instructions_section=dev_instructions_section,
        )

    def _build_code_review_reader_prompt(self, pr_number, branch_name, comment):
        file_path = comment.get('path', 'N/A')
        line = comment.get('line', comment.get('original_line', 'N/A'))
        body = comment.get('body', '')
        reviewer = comment.get('user', {}).get('login', 'unknown')
        diff_hunk = comment.get('diff_hunk', '')

        template = self._load_prompt("code_review_reader.txt")
        return template.format(
            pr_number=pr_number,
            branch_name=branch_name,
            file_path=file_path,
            line=line,
            body=body,
            reviewer=reviewer,
            diff_hunk=diff_hunk,
        )

    def _build_code_review_reader_batch_prompt(self, pr_number, branch_name, comments, dev_instructions="", pr_diff=""):
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
            
            # Include thread replies for full conversation context
            thread_replies = comment.get('_thread_replies', [])
            if thread_replies:
                comments_section += f"\nThread conversation:\n"
                for reply in thread_replies:
                    reply_author = reply.get('user', {}).get('login', 'unknown')
                    reply_body = reply.get('body', '')
                    comments_section += f"  {reply_author}: {reply_body}\n"
            
            # Include retry hint if this is a retry attempt
            retry_hint = comment.get('_retry_hint', '')
            if retry_hint:
                comments_section += f"\n⚠️ {retry_hint}\n"

            comments_section += f"Diff context:\n{diff_hunk}\n"

        dev_instructions_section = f"\nREPOSITORY DEV INSTRUCTIONS (follow these when running commands, tests, etc.):\n{dev_instructions}\n" if dev_instructions else ""

        pr_diff_section = ""
        if pr_diff:
            # Truncate diff to avoid exceeding context limits
            truncated = pr_diff[:self.MAX_PR_DIFF_LENGTH] + "\n... (truncated)" if len(pr_diff) > self.MAX_PR_DIFF_LENGTH else pr_diff
            pr_diff_section = f"\nPR DIFF (changes in this PR vs main — use this to understand what was changed):\n```\n{truncated}\n```\n"

        template = self._load_prompt("code_review_reader_batch.txt")
        return template.format(
            num_comments=len(comments),
            pr_number=pr_number,
            branch_name=branch_name,
            comments_section=comments_section,
            dev_instructions_section=dev_instructions_section,
            pr_diff_section=pr_diff_section,
        )

    def _build_local_review_fix_prompt(self, issue_key, description, review_feedback, branch_name, iteration, dev_instructions=""):
        dev_instructions_section = f"\nREPOSITORY DEV INSTRUCTIONS (follow these when running commands, tests, etc.):\n{dev_instructions}\n" if dev_instructions else ""

        template = self._load_prompt("local_review_fix.txt")
        return template.format(
            issue_key=issue_key,
            branch_name=branch_name,
            iteration=iteration,
            description=description,
            review_feedback=review_feedback,
            dev_instructions_section=dev_instructions_section,
        )

    def _build_local_review_prompt(self, issue_key, description, diff, branch_name):
        template = self._load_prompt("local_review.txt")
        return template.format(
            issue_key=issue_key,
            branch_name=branch_name,
            description=description,
            diff=diff,
        )

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

        # Check for CODE_CHANGED: format (used in review fix responses for consistency)
        if output.startswith("CODE_CHANGED:"):
            self.logger.info("Detected: CODE_CHANGED format (in progress)")
            return {"action": "in_progress", "message": output, "pr_number": None, "pr_url": None}

        # Check for PR creation FIRST — this takes priority over everything else
        # CRITICAL: Only return success if we have an ACTUAL PR number or URL, not just the words "pull request"
        pr_match = re.search(r'pr\s*#?(\d+)', output_lower)
        pr_url_match = re.search(r'(https://github\.com/[^\s]+/pull/\d+)', output, re.IGNORECASE)
        
        # Only return success if we have CONCRETE evidence of a PR (number OR URL)
        if pr_match or pr_url_match:
            pr_number = int(pr_match.group(1)) if pr_match else None
            pr_url = pr_url_match.group(1) if pr_url_match else None
            self.logger.info(f"Detected: PR #{pr_number} at {pr_url}")
            return {"action": "success", "pr_number": pr_number, "pr_url": pr_url, "message": output}

        # Check for explicit DONE statement with PR
        if output.startswith("DONE:") and ('pr #' in output_lower or 'github.com' in output_lower):
            self.logger.info("Detected: DONE with PR reference")
            return {"action": "success", "pr_number": None, "pr_url": None, "message": output}

        # Check for explicit STUCK statement
        if output.startswith("STUCK:"):
            self.logger.info("Detected: explicit STUCK statement")
            return {"action": "stuck", "message": output, "pr_number": None, "pr_url": None}

        # Check for failure/stuck keywords
        if any(kw in output_lower for kw in ['tests failed', 'test failed', 'implementation incomplete']):
            self.logger.info("Detected: tests failed (no PR created)")
            return {"action": "stuck", "message": output, "pr_number": None, "pr_url": None}
        if any(kw in output_lower for kw in ['stuck', 'cannot', 'unable to', 'blocked']):
            self.logger.info("Detected: stuck/blocked")
            return {"action": "stuck", "message": output, "pr_number": None, "pr_url": None}
        
        # Default: still in progress (agent needs to continue working)
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
