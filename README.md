# Brad - Autonomous AI Software Engineer

Brad is an autonomous AI software engineer that takes JIRA issues labeled with `BradReview`, implements them using an LLM-powered coding agent, and delivers production-ready code via pull requests.

## Architecture

Brad is a **thin orchestration layer** that delegates all engineering decisions to an AI coding agent. Brad uses an **adapter pattern** to abstract all external integrations, making it straightforward to swap ticketing systems, code repositories, CI/CD providers, observability tools, and LLM backends.

### Adapter Architecture

| Layer | Abstract Interface | Current Implementation |
|---|---|---|
| **Ticketing** | `TicketingAdapter` | Jira |
| **Code Repository** | `CodeRepositoryAdapter` | GitHub |
| **CI/CD** | `CICDAdapter` | GitHub Actions |
| **Observability** | `ObservabilityAdapter` | Azure AKS |
| **LLM** | `LLMAdapter` | Azure OpenAI Responses API |

### Project Structure

```
brad.py                          # CLI entry point
brad_gui.py                      # Web GUI entry point
brad/                            # Main package
├── orchestrator.py              # Core workflow engine
├── config.py                    # Configuration management
├── db.py                        # SQLite persistence (history, costs)
├── logging_config.py
├── repo_manager.py              # Git operations (standalone)
├── adapters/                    # Adapter layer
│   ├── ticketing/               # e.g. Jira
│   ├── code_repository/         # e.g. GitHub
│   ├── ci_cd/                   # e.g. GitHub Actions
│   ├── observability/           # e.g. Azure AKS
│   └── llm/                     # e.g. Azure OpenAI
├── agents/                      # Prompt building & response parsing
│   └── interface.py
└── gui/                         # Flask read-only dashboard
    ├── app.py
    ├── static/
    └── templates/
tests/                           # Unit tests
```

## Prerequisites

1. **Python 3.12+**
2. **Azure OpenAI** resource with a deployed model
3. **Git**
4. **JIRA Account** with API access
5. **GitHub Account** with API access
6. **Target Repository** — the repository Brad will work on

## Installation

```bash
git clone <brad-repo-url>
cd brad
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill in your values. See `.env.example` for all options.

**Required:**
- `JIRA_URL`, `JIRA_USER`, `JIRA_TOKEN`
- `GITHUB_TOKEN`, `GITHUB_REPO`
- `TARGET_REPO_PATH` — absolute path to the repo Brad works on
- `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_MODEL`

**Optional (cost tracking):**
- `LLM_COST_PER_1K_PROMPT_TOKENS`, `LLM_COST_PER_1K_COMPLETION_TOKENS`

## Usage

### Run Brad (process issues once)

```bash
python brad.py run --once
```

### Start Web GUI

```bash
;;
```

The GUI shows execution history, per-ticket cost breakdowns, CI/CD status, and real-time backend status.

### Debug Logging

```bash
python brad.py run --once --log-level DEBUG
```

## Workflow

1. **PM creates JIRA issue** with description + attachments, adds label `BradReview`
2. **Brad analyzes requirements** — may request clarification or propose test scenarios
3. **Brad implements** — creates feature branch, writes code + tests, opens PR
4. **Brad monitors CI/CD** — fixes failures (up to 5 attempts), addresses review comments
5. **Brad completes** — sets JIRA status to REVIEW, posts "Brad is done."

## Safety Features

- **Max clarification cycles:** 3 per issue
- **Max CI fix iterations:** 5 per issue
- **Max review fix iterations:** 3 per issue
- **Agent iteration limit:** 200 per LLM invocation
- **No scope changes** — Brad never modifies requirements
- **No auto-merge** — all PRs require human review

## Testing

```bash
pytest tests/ -v
pytest tests/ --cov=brad --cov-report=html
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add new adapters and extend Brad.

## License

[MIT](LICENSE)
