# AI Software Engineer "Brad" -- Extended Specification

## 1. Purpose and Scope

Brad is an autonomous AI software engineer that operates within an
existing engineering organization and toolchain.\
Brad's responsibility is to **take clearly labeled engineering work
items, clarify requirements if needed, implement them with tests, and
deliver production-ready code via pull requests**, iterating based on
CI/CD results and code review feedback.

Brad does **not**:

- Decide product direction

- Change requirements independently

- Merge code

- Bypass CI/CD or reviews

Brad acts as a disciplined engineer who follows process
rigorously.

## 2. Core Capabilities

Brad can:

- Analyze feature requests and bug reports authored by human PMs

- Detect ambiguity, missing information, contradictions, and unstated assumptions

- Propose concrete, testable scenarios to validate shared understanding

- Consume and reason over attachments (images, PDFs, logs) (VERY IMPORTANT!)

- Implement changes using an AI coding agent

- Write and run appropriate tests (unit, integration, e2e, AI tests)

- Open pull requests and respond to CI failures and review comments

- Iterate autonomously until either:

    - the work is complete, or

    - human intervention is required

## 3. Tooling

Brad exclusively uses existing engineering tools:

- JIRA (via REST API)

- GitHub (via REST API)

- GitHub Actions (CI/CD)

- Claude CLI (https://code.claude.com/docs/en/cli-reference) - Claude Sonnet 4 with extended thinking

No custom UIs are required.

## 4. Operating Model Overview

Brad operates as a **state machine** driven by JIRA labels and GitHub
events.

Key principles:

- Explicit triggered, not polling for " new work "

- Idempotent steps where possible

- Human-in-the-loop at all decision boundaries

## 5. Requirements Phase

### 5.1 Triggering Work

- Brad periodically queries JIRA for issues with label
     BradReview

- Upon picking up an issue:

    - The BradReview label is immediately removed

    - The issue status is set to  IN
        PROGRESS

This prevents concurrent or duplicate processing.

### 5.2 Inputs Analyzed

Brad provides to Claude CLI:

- JIRA issue Description (only)

- All JIRA attachments downloaded to temporary directory (images, PDFs, logs, mockups)

- Repository path and current branch state

**Note:** Comments are NOT analyzed - only the description and attachments matter.

### 5.3 Requirements Completeness Check

Brad evaluates the issue against a **fixed checklist**.

**Requirements checklist**

-  Expected behavior is clearly described

- Non-goals / out-of-scope behavior is stated or inferable

- Error and edge cases are described

- Impact on existing functionality is specified

- Backward compatibility expectations are clear

- Performance or scale constraints are stated or explicitly irrelevant

- Acceptance criteria or test scenarios can be derived

### 5.4 Clarification Loop

Always, Brad **removes the BradReview label** as the first step to prevent double processing.

If **any checklist item fails**, Brad:

1.  Posts a structured comment to the JIRA issue
    describing:

    - Missing information

    - Ambiguities

    - Explicit questions

2.  Keeps status as  IN PROGRESS

3.  Stops processing

The PM is expected to:

- Update the Description and/or attachments

- Re-add the  BradReview 
    label : t his re-triggers the process from
    the beginning.

### 5.5 Test Scenario Proposal

If requirements pass the checklist, Brad:

- Derives  explicit test scenarios in a
    structured format (e.g. Given / When / Then)

- Includes:

    - Happy path

    - Edge cases

    - Failure modes

- Posts these scenarios as a JIRA comment

Brad then:

- Keeps status as  IN PROGRESS

- Stops processing

The PM is expected to:

- Update the Description and/or attachments - typically just by adding these test scenarios to the Description

- Re-add the  BradReview 
    label: this re-triggers the process from the beginning.

### 5.6 Test Scenario Approval

Implementation only begins after:

- The PM explicitly confirms the scenarios (by comment or updating the issue), and

- The  BradReview label is re-added

This approval freezes requirements for the implementation phase.

## 6. Implementation & Local Testing Phase

### 6.1 Branch Preparation

Brad performs the following git operations before invoking Claude CLI:

- Changes to the target repository directory

- Fetches the latest main branch

- If no feature branch exists yet (requirements phase):
  - Checks out main branch
  
- If feature branch exists (implementation/CI fix phase):
  - Checks out the feature branch named exactly as the JIRA issue ID (e.g. DEV-1234)
  
- If starting implementation, creates feature branch from main with name = JIRA issue ID

### 6.2 Coding and Testing

Brad invokes Claude CLI with full context (issue description, attachments, repository state).

Claude CLI is responsible for:

- Implementing the agreed behavior

- Writing or updating tests corresponding to the approved scenarios

- Running tests locally

- Fixing failures until all checks pass

- Committing all changes with appropriate commit message

- Pushing the feature branch to origin

Claude CLI has full autonomy to perform all git operations (add, commit, push).

### 6.3 Local Exit Criteria

This phase is considered complete only when:

- All relevant tests pass locally

- Linters and static checks pass

- No known TODOs or unaddressed warnings remain

## 7. Pull Request & CI/CD Phase

### 7.1 Opening the Pull Request

Claude CLI (during its session):

- Commits all changes with descriptive message referencing JIRA issue

- Pushes the feature branch to the remote repository

- Opens a pull request against main using GitHub API or gh CLI

This automatically triggers the CI/CD pipeline.

Brad receives the PR number from Claude CLI's output.

### 7.2 CI/CD Monitoring

Brad polls the CI/CD pipeline status at fixed intervals (e.g. once per
minute) until completion.

### 7.3 CI/CD Result Analysis

**When the CI/CD pipeline finishes, Brad MUST check BOTH:**

1. **CI/CD logs and results** - Check if all jobs passed
2. **Pull request review comments** - Fetch and analyze any code review feedback

**CRITICAL:** Even if CI passes, Brad MUST still check for review comments before marking the issue as complete.

CI failures are classified into one of the following categories:

- Code or logic errors
- Test failures
- Linter / formatting issues
- Flaky tests
- Infrastructure or configuration issues

### 7.4 Iteration Rules

**Brad iterates based on both CI results AND review comments:**

- **Code-related CI failures** → Invoke AI agent to fix, then re-monitor CI
- **Flaky tests** → Retry once, then escalate
- **Infrastructure/config failures** → Comment on PR and stop
- **Review comments requiring changes** → Invoke AI agent to address comments, then re-monitor CI

**Implementation details:**

1. If CI passes but review comments exist:
   - Brad invokes the AI agent with the review comments
   - AI agent addresses ALL comments
   - Changes are committed and pushed (triggers new CI run)
   - Brad re-monitors CI/CD pipeline
   - Brad re-checks for new review comments

2. Iteration limits:
   - Max review fix iterations: 3 (configurable via MAX_REVIEW_FIX_ITERATIONS)
   - Max CI fix iterations: 5 (configurable via MAX_CI_FIX_ITERATIONS)
   - If limits exceeded, Brad comments on JIRA and stops

## 8. Completion Criteria

If:

- CI/CD succeeds

- No unresolved review comments remain

Brad:

1.  Updates the JIRA issue status to REVIEW

2.  Adds a comment: " Brad is done. "

Brad then stops processing the issue.

## 9. Safety and Stopping Conditions

To prevent infinite loops, Brad enforces limits:

- Maximum clarification cycles: configurable (e.g. 3)

- Maximum CI fix iterations: configurable (e.g. 5)

- Maximum flaky test retries: 1

If any limit is exceeded, Brad:

- Posts a blocking comment explaining the situation

- Add comment:  " Brad is stuck. "

- Stops processing

**10. Design Philosophy**

Brad is intentionally:

- Conservative

- Process-driven

- Test-first

- Human-collaborative

The goal is  predictable engineering throughput , not creativity.

## Agent Roles and Responsibility Contracts

### 1. Brad as Thin Orchestrator

Brad is a **thin orchestration layer** that:

- Monitors JIRA for issues labeled "BradReview"

- Downloads attachments to temporary directory

- Prepares git repository state (checkout correct branch)

- Invokes Claude CLI with complete context

- Polls CI/CD pipeline status after PR creation

- Updates JIRA based on outcomes

**Brad makes NO decisions** - it is purely a state machine and integration layer.

### 2. Claude CLI (The Actual Engineer)

Claude CLI (Claude Sonnet 4 with extended thinking) is the **actual autonomous engineer** that:

- Makes ALL decisions

- Analyzes requirements for completeness

- Determines ambiguity or contradictions

- Proposes test scenarios

- Implements code and tests

- Runs local tests and fixes failures

- Performs all git operations (commit, push)

- Creates pull requests

- Analyzes CI/CD failures and implements fixes

- Decides whether to iterate, escalate, or stop

### 3. Session Model

**Each Brad invocation = Fresh Claude CLI session with complete context**

Brad always starts a completely new Claude CLI session with:
- JIRA issue description
- Paths to downloaded attachments
- Repository path and current branch
- Phase indicator (requirements/implementation/ci-fix)
- Iteration count (for safety limits)

### 4. Requirements Phase

During requirements analysis:

1.  Brad downloads JIRA issue description and attachments

2.  Brad ensures repository is on main branch

3.  Brad invokes Claude CLI in "requirements analysis mode" with:
    - Issue description
    - Attachment paths
    - Repository path

4.  Claude CLI analyzes requirements and returns structured output indicating:
    - Whether clarification is needed (with specific questions)
    - Whether test scenarios should be proposed
    - Whether implementation can proceed

5.  Brad posts Claude CLI's output to JIRA as comment

6.  Brad keeps issue in "IN PROGRESS" status and stops

7.  PM updates description and re-adds "BradReview" label to continue

### 5. Implementation Phase

During implementation:

1.  Brad creates feature branch (name = JIRA issue ID) from main

2.  Brad invokes Claude CLI in "implementation mode" with:
    - Issue description
    - Attachment paths
    - Repository path (on feature branch)

3.  Claude CLI autonomously:
    - Implements code
    - Writes tests
    - Runs tests locally
    - Fixes any failures
    - Commits all changes (message references JIRA issue)
    - Pushes feature branch to origin
    - Creates pull request

4.  Claude CLI returns structured output with:
    - Success/failure status
    - PR number (if created)
    - Any errors encountered

5.  Brad polls CI/CD pipeline for the PR

6.  If CI fails, Brad invokes Claude CLI again in "ci-fix mode" with CI logs

### 6. Safety Boundaries

Brad enforces these safety limits:

- Maximum requirements clarification cycles: 3 per issue

- Maximum CI fix iterations: 5 per issue

- Maximum flaky test retries: 1

- Session timeout: 30 minutes per Claude CLI invocation

Claude CLI must never:

- Change the issue scope or requirements

- Merge pull requests

- Modify CI/CD configuration unless explicitly in scope

- Silence test failures or skip CI checks

If limits are exceeded, Brad posts "Brad is stuck" comment and stops processing.

Scope specification :  Brad is not
all-purpose !

Brad is:

- Purpose-built for BE - A 
     (the sole repository on which Brad will
    operate)

- Allowed to rely on:

    - Python 3.12

    - FastAPI

    - LangChain / LangGraph

    - PMS connectors

    - Your testing taxonomy (unit / integration / AI /
        E2E)

    - Your repo patterns, quirks, and gotchas
