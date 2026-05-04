"""Gather context for AI-driven rebase conflict resolution.

This module is *deterministic context-gathering only* — no editing, no
heuristics that decide how a conflict should be resolved. The AI agent
receives the raw conflict markers plus a structured digest of:

- both sides' base/ours/theirs blobs
- ``git blame`` on the conflicting line ranges (both sides)
- commit messages of the commits that introduced the conflicting lines
- when reachable: the merged main-side PR title/body, plus Jira ticket
  summary+description for both the main-side change and the feature branch

Every external lookup is best-effort: lookup failures log a warning and
are omitted from the context, never raised. The agent is expected to
work with whatever is available.
"""

import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional


JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
CONFLICT_HUNK_RE = re.compile(
    r"^<{7} .*?$.*?^={7}.*?^>{7} .*?$",
    re.MULTILINE | re.DOTALL,
)


@dataclass
class JiraSnippet:
    key: str
    summary: str = ""
    description: str = ""

    def to_text(self) -> str:
        out = [f"[{self.key}] {self.summary}".strip()]
        if self.description:
            desc = self.description
            if len(desc) > 2000:
                desc = desc[:2000] + " …(truncated)"
            out.append(desc)
        return "\n".join(out)


@dataclass
class FileConflictContext:
    path: str
    working_copy: str = ""           # current file content with markers
    base_blob: str = ""              # :1: stage (merge base)
    ours_blob: str = ""              # :2: stage (HEAD/feature)
    theirs_blob: str = ""            # :3: stage (incoming/main)
    blame_ours: str = ""
    blame_theirs: str = ""
    log_ours: str = ""               # commits on feature side touching this file
    log_theirs: str = ""             # commits on main side touching this file
    related_main_prs: List[Dict] = field(default_factory=list)
    related_main_jira: List[JiraSnippet] = field(default_factory=list)


@dataclass
class ConflictContext:
    branch_name: str
    base_branch: str
    feature_jira: Optional[JiraSnippet] = None
    files: List[FileConflictContext] = field(default_factory=list)

    def to_prompt_section(self) -> str:
        """Render this context as a prompt-ready string."""
        parts: List[str] = []
        parts.append(f"Branch: {self.branch_name}")
        parts.append(f"Rebasing onto: origin/{self.base_branch}")

        if self.feature_jira:
            parts.append("\n## Feature branch Jira ticket")
            parts.append(self.feature_jira.to_text())

        for f in self.files:
            parts.append(f"\n## Conflicted file: {f.path}")

            if f.log_ours.strip():
                parts.append("\n### Recent feature-branch commits touching this file (ours)")
                parts.append(_truncate(f.log_ours, 2500))
            if f.log_theirs.strip():
                parts.append("\n### Recent main-branch commits touching this file (theirs)")
                parts.append(_truncate(f.log_theirs, 2500))

            if f.related_main_prs:
                parts.append("\n### Merged main-side PRs that introduced the conflicting lines")
                for pr in f.related_main_prs[:5]:
                    parts.append(
                        f"- PR #{pr.get('number')}: {pr.get('title','')}\n"
                        f"  branch: {pr.get('head', {}).get('ref','')}\n"
                        f"  url: {pr.get('html_url','')}"
                    )
            if f.related_main_jira:
                parts.append("\n### Jira tickets behind the main-side change")
                for j in f.related_main_jira:
                    parts.append(j.to_text())

            if f.blame_ours.strip():
                parts.append("\n### git blame on conflicting lines (ours / feature)")
                parts.append("```\n" + _truncate(f.blame_ours, 2000) + "\n```")
            if f.blame_theirs.strip():
                parts.append("\n### git blame on conflicting lines (theirs / main)")
                parts.append("```\n" + _truncate(f.blame_theirs, 2000) + "\n```")

            parts.append("\n### Working-copy content (with conflict markers)")
            parts.append("```\n" + _truncate(f.working_copy, 6000) + "\n```")

        return "\n".join(parts)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(truncated)"


def extract_jira_key(text: str) -> Optional[str]:
    """Pull the first Jira-style key (e.g. DEV-123) out of a string."""
    if not text:
        return None
    m = JIRA_KEY_RE.search(text)
    return m.group(1) if m else None


def find_conflict_line_ranges(content: str) -> List[Dict[str, int]]:
    """Return [{ours_start, ours_end, theirs_start, theirs_end}] line ranges (1-indexed).

    Lines are counted in the *working copy* (with markers). For the ours-side
    we report the lines between ``<<<<<<<`` and ``=======``; for the theirs
    side the lines between ``=======`` and ``>>>>>>>``. Markers themselves are
    excluded from the ranges.
    """
    ranges: List[Dict[str, int]] = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith("<<<<<<<"):
            ours_start = i + 2  # 1-indexed, skip marker line
            j = i + 1
            while j < len(lines) and not lines[j].startswith("======="):
                j += 1
            if j >= len(lines):
                break
            ours_end = j  # line of ======= is j+1; ours_end = j (1-indexed end-inclusive)
            theirs_start = j + 2
            k = j + 1
            while k < len(lines) and not lines[k].startswith(">>>>>>>"):
                k += 1
            if k >= len(lines):
                break
            theirs_end = k  # 1-indexed end-inclusive
            ranges.append({
                "ours_start": ours_start,
                "ours_end": max(ours_start, ours_end),
                "theirs_start": theirs_start,
                "theirs_end": max(theirs_start, theirs_end),
            })
            i = k + 1
        else:
            i += 1
    return ranges


def gather_context(
    repo,
    branch_name: str,
    base_branch: str,
    conflicted_files: List[str],
    code_repo=None,
    ticketing=None,
    logger=None,
) -> ConflictContext:
    """Build a :class:`ConflictContext` for the given conflicted files.

    Parameters
    ----------
    repo:
        :class:`brad.repo_manager.RepoManager` (uses ``read_conflicted_file``,
        ``show_stage_blob``, ``blame_range``, ``log_messages``).
    code_repo:
        Optional code-repo adapter (e.g. ``GitHubAdapter``). When it has
        ``get_prs_for_commit``, blame SHAs from main are mapped back to the
        merged PR for richer context.
    ticketing:
        Optional ticketing adapter (e.g. ``JiraAdapter``). When it has
        ``fetch_issue``, Jira keys discovered in branch names / PR titles /
        commit messages are fetched and included.
    """
    log = logger
    feature_jira = None
    if ticketing is not None:
        key = extract_jira_key(branch_name)
        if key:
            feature_jira = _fetch_jira(ticketing, key, log)

    file_ctxs: List[FileConflictContext] = []
    for path in conflicted_files:
        fc = FileConflictContext(path=path)
        try:
            fc.working_copy = repo.read_conflicted_file(path)
            fc.base_blob = repo.show_stage_blob(1, path)
            fc.ours_blob = repo.show_stage_blob(2, path)
            fc.theirs_blob = repo.show_stage_blob(3, path)

            # Commit history on each side touching this file. We compare the
            # current rebase HEAD (== ours, mid-rebase) and the incoming side
            # (the rebase target, ``origin/<base_branch>``) against the merge
            # base. Best-effort: any failure leaves the field empty.
            fc.log_ours = repo.log_messages(
                f"origin/{base_branch}..HEAD", path, max_commits=5
            )
            fc.log_theirs = repo.log_messages(
                f"HEAD..origin/{base_branch}", path, max_commits=5
            )

            ranges = find_conflict_line_ranges(fc.working_copy)
            if ranges:
                # Blame on ours: blame the *staged ours-blob* checked into the
                # index. Easiest proxy: blame HEAD on the same path. For
                # theirs: blame origin/<base_branch> on the same path.
                ours_lines: List[str] = []
                theirs_lines: List[str] = []
                main_blame_shas: List[str] = []
                for r in ranges[:5]:  # cap per file
                    ob = repo.blame_range("HEAD", path, r["ours_start"], r["ours_end"])
                    if ob:
                        ours_lines.append(ob)
                    tb = repo.blame_range(
                        f"origin/{base_branch}", path,
                        r["theirs_start"], r["theirs_end"],
                    )
                    if tb:
                        theirs_lines.append(tb)
                        for sha in _extract_blame_shas(tb):
                            if sha not in main_blame_shas:
                                main_blame_shas.append(sha)
                fc.blame_ours = "\n".join(ours_lines)
                fc.blame_theirs = "\n".join(theirs_lines)

                if main_blame_shas and code_repo is not None and hasattr(code_repo, "get_prs_for_commit"):
                    seen_pr_numbers = set()
                    seen_jira_keys = set()
                    for sha in main_blame_shas[:5]:
                        try:
                            prs = code_repo.get_prs_for_commit(sha)
                        except Exception as e:
                            if log:
                                log.warning(f"PR lookup failed for {sha[:8]}: {e}")
                            continue
                        for pr in prs:
                            num = pr.get("number")
                            if num in seen_pr_numbers:
                                continue
                            seen_pr_numbers.add(num)
                            fc.related_main_prs.append(pr)
                            if ticketing is not None:
                                jkey = (
                                    extract_jira_key(pr.get("head", {}).get("ref", ""))
                                    or extract_jira_key(pr.get("title", ""))
                                    or extract_jira_key(pr.get("body", "") or "")
                                )
                                if jkey and jkey not in seen_jira_keys:
                                    seen_jira_keys.add(jkey)
                                    snippet = _fetch_jira(ticketing, jkey, log)
                                    if snippet:
                                        fc.related_main_jira.append(snippet)
        except Exception as e:
            if log:
                log.warning(f"conflict_context: failed to gather for {path}: {e}")

        file_ctxs.append(fc)

    return ConflictContext(
        branch_name=branch_name,
        base_branch=base_branch,
        feature_jira=feature_jira,
        files=file_ctxs,
    )


def _extract_blame_shas(blame_output: str) -> List[str]:
    """Pull the leading SHA from each line of ``git blame`` output."""
    shas: List[str] = []
    for line in blame_output.splitlines():
        # blame lines look like "abcd1234 (Author 2024-...) ..." — take the
        # leading hash, stripping any leading "^" that marks a boundary commit.
        token = line.split(" ", 1)[0].lstrip("^")
        if len(token) >= 7 and all(c in "0123456789abcdef" for c in token.lower()):
            shas.append(token)
    return shas


def _fetch_jira(ticketing, key: str, logger) -> Optional[JiraSnippet]:
    """Best-effort Jira lookup. Returns ``None`` on any failure."""
    try:
        if not hasattr(ticketing, "fetch_issue"):
            return None
        issue = ticketing.fetch_issue(key)
        if not issue:
            return None
        fields = issue.get("fields", {}) if isinstance(issue, dict) else {}
        summary = fields.get("summary", "") or ""
        desc = fields.get("description", "") or ""
        # Description may be ADF; lazy-import to keep this module light.
        if isinstance(desc, dict):
            try:
                from brad.adf_parser import adf_to_text
                desc = adf_to_text(desc)
            except Exception:
                desc = ""
        return JiraSnippet(key=key, summary=summary, description=desc)
    except Exception as e:
        if logger:
            logger.warning(f"Jira lookup failed for {key}: {e}")
        return None
