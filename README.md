# Brad - Autonomous AI Software Engineer

Brad is an autonomous AI software engineer that takes JIRA issues labeled with `BradReview`, implements them using an AI coding agent (Claude Code or OpenCode CLI), and delivers production-ready code via pull requests.

## Architecture

Brad is a **thin orchestration layer** that delegates all engineering decisions to Claude CLI (Claude Sonnet 4 with extended thinking) or OpenCode CLI. Brad's responsibilities:

- Monitor JIRA for issues labeled "BradReview"
- Download attachments and prepare git repository state
- Invoke Claude CLI with complete context
- Update JIRA based on outcomes
- Monitor CI/CD pipelines

**Claude CLI does all the thinking:**
- Analyzes requirements for completeness
- Proposes test scenarios
- Implements code and tests
- Fixes CI/CD failures
- Makes all engineering decisions

## Prerequisites

1. **Python 3.12+**
2. **Claude CLI** - Install from https://code.claude.com/docs/en/cli-reference
3. **Git** - For repository operations
4. **JIRA Account** with API access
5. **GitHub Account** with API access
6. **Target Repository** - The repository Brad will work on

## Installation

### 1. Clone Brad Repository

```bash
git clone <brad-repo-url>
cd brad
```

### 2. Create Virtual Environment

```bash
python -m venv .venv
```

### 3. Activate Virtual Environment

**Windows (PowerShell):**
```powershell
.venv\Scripts\Activate.ps1
```

**Windows (Git Bash):**
```bash
source .venv/Scripts/activate
```

**Linux/macOS:**
```bash
source .venv/bin/activate
```

### 4. Install Dependencies

```bash
pip install -r requirements.txt
```

### 5. Install AI CLI (Claude CLI or OpenCode)

**Choose ONE of the following:**

#### Option A: Claude CLI
Follow instructions at: https://code.claude.com/docs/en/cli-reference

Verify installation:
```bash
claude --version
```

#### Option B: OpenCode
Follow instructions at: https://github.com/stackblitz/opencode

Verify installation:
```bash
opencode --version
```

### 6. **REQUIRED:** Clone and Set Up the Target Repository (flaerobotics/bea)

**This step is mandatory.** Brad works on a target repository that must be cloned and properly configured:

1. Clone the flaerobotics/bea repository:
```bash
git clone https://github.com/flaerobotics/bea.git /path/to/your/bea
cd /path/to/your/bea
```

2. Set up the repository environment:
```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies using uv
uv sync
```

3. **Configure your AI CLI to work in this repository:**
   - Ensure Claude CLI or OpenCode is configured to operate within the cloned repository path
   - The AI agent will execute commands and make changes in this directory
   - This path will be specified as `TARGET_REPO_PATH` in your `.env` configuration (see Configuration section below)

## Configuration

Create a `.env` file or set environment variables:

### Required Variables

```bash
# JIRA Configuration
JIRA_URL=https://your-company.atlassian.net
JIRA_USER=your-email@company.com
JIRA_TOKEN=your-jira-api-token

# GitHub Configuration
GITHUB_TOKEN=your-github-token
GITHUB_REPO=flaerobotics/bea  # Must be the flaerobotics/bea repository (set up in step 6)

# Target Repository (must be the absolute path to the cloned flaerobotics/bea repo from step 6)
TARGET_REPO_PATH=/path/to/your/bea  # e.g., /home/user/projects/bea or C:/git/bea

# AI Agent Configuration
# Options: "claude" or "opencode"
AI_AGENT=opencode

# Claude CLI Configuration
CLAUDE_CLI_PATH=claude

# OpenCode CLI Configuration
OPENCODE_CLI_PATH=opencode  # or full path if not in PATH
```

### Optional Variables

```bash
# JIRA Project Key (default: DEV)
JIRA_PROJECT_KEY=DEV

# Safety Limits
MAX_CLARIFICATION_CYCLES=3
MAX_CI_FIX_ITERATIONS=5
MAX_REVIEW_FIX_ITERATIONS=3  # Max iterations to address PR review comments
MAX_FLAKY_RETRIES=1
CI_POLL_INTERVAL=60

# Logging
LOG_LEVEL=INFO
ATTACHMENTS_DIR=./attachments
```

### Getting API Tokens

**JIRA API Token:**
1. Go to https://id.atlassian.com/manage-profile/security/api-tokens
2. Create new token
3. Copy the token

**GitHub Token:**
1. Go to https://github.com/settings/tokens
2. Generate new token (classic)
3. Required scopes: `repo`, `workflow`
4. Copy the token

## Usage

### Process Issues Once

```bash
python brad.py run --once
```

### Enable Debug Logging

```bash
python brad.py run --once --log-level DEBUG
```

### Check Logs

Logs are written to `logs/brad_<timestamp>.log`

```bash
tail -f logs/brad_*.log
```

## Workflow

### 1. PM Creates JIRA Issue

- Create issue with clear description
- Add attachments (mockups, diagrams, logs, etc.)
- Add label: **BradReview**

### 2. Brad Analyzes Requirements

Brad removes the label immediately and analyzes the requirements. Brad may:

- **Request clarification** if requirements are incomplete
- **Propose test scenarios** for PM approval
- **Proceed to implementation** if everything is clear

PM must:
- Update the description based on feedback
- Re-add **BradReview** label to continue

### 3. Brad Implements

Brad:
- Creates feature branch (name = JIRA issue key, e.g., DEV-1234)
- Implements code and tests
- Commits and pushes changes
- Creates pull request

### 4. Brad Monitors CI/CD

Brad waits for CI/CD to complete. If CI fails, Brad:
- Analyzes failure logs
- Fixes the issues
- Pushes fixes (CI re-runs automatically)
- Repeats up to 5 times

### 5. Brad Completes

When CI passes, Brad:
- Sets JIRA status to **REVIEW**
- Posts comment: "Brad is done. ✅"

## Safety Features

- **Maximum clarification cycles:** 3 per issue
- **Maximum CI fix iterations:** 5 per issue
- **Session timeout:** 30 minutes per Claude CLI invocation
- **No scope changes** - Brad never modifies requirements
- **No auto-merge** - All PRs require human review

## Testing

### Run Unit Tests

```bash
pytest tests/
```

### Run with Coverage

```bash
pytest tests/ --cov=. --cov-report=html
```

### Run Specific Test

```bash
pytest tests/test_config.py -v
```

## Development

### Code Style

```bash
# Format code
black .

# Lint
flake8 .

# Type checking
mypy .
```

### Adding New Features

1. Update specification in `AI Software Engineer.md`
2. Implement changes
3. Add unit tests
4. Update README

## Troubleshooting

### Brad is stuck

Check the log file for details:
```bash
cat logs/brad_*.log | grep ERROR
```

Common issues:
- Invalid JIRA workflow transitions
- GitHub API rate limits
- Claude CLI timeout
- Git conflicts

### Claude CLI not found

Ensure Claude CLI is installed and in PATH:
```bash
which claude  # Linux/macOS
where claude  # Windows
```

### JIRA API errors

Verify credentials:
```bash
curl -u your-email@company.com:your-token https://your-company.atlassian.net/rest/api/3/myself
```

### GitHub API errors

Verify token:
```bash
curl -H "Authorization: token your-token" https://api.github.com/user
```

## Architecture Details

See `AI Software Engineer.md` for complete specification.

**Key Components:**
- `brad.py` - Main entry point
- `brad_orchestrator.py` - State machine and workflow logic
- `claude_interface.py` - Claude CLI integration
- `jira_client.py` - JIRA API client
- `github_client.py` - GitHub API client
- `ci_analyzer.py` - CI/CD monitoring
- `repo_manager.py` - Git operations
- `config.py` - Configuration management
- `logging_config.py` - Logging setup

## License

[Your License Here]

## Support

For issues or questions, contact: [Your Contact Info]
