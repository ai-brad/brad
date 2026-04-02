# Contributing to Brad

Thank you for your interest in contributing to Brad! This guide covers how to add new adapters and extend Brad's capabilities.

## Architecture Overview

Brad uses an **adapter pattern** to abstract external service integrations. Each adapter category has:

1. An **abstract base class** in `brad/adapters/<category>/base.py`
2. One or more **concrete implementations** (e.g. `jira_adapter.py`, `github_adapter.py`)
3. The orchestrator in `brad/orchestrator.py` uses adapters via their abstract interfaces

### Adapter Categories

| Category | Base Class | Purpose |
|---|---|---|
| **Ticketing** | `brad/adapters/ticketing/base.py` | Issue tracking (Jira, Linear, etc.) |
| **Code Repository** | `brad/adapters/code_repository/base.py` | PR management (GitHub, GitLab, etc.) |
| **CI/CD** | `brad/adapters/ci_cd/base.py` | Pipeline monitoring (GitHub Actions, Jenkins, etc.) |
| **Observability** | `brad/adapters/observability/base.py` | Deployment logs/health (Azure, Datadog, etc.) |
| **LLM** | `brad/adapters/llm/base.py` | AI coding agent (Azure OpenAI, OpenAI, local, etc.) |

## Adding a New Adapter

### Step 1: Create the adapter file

Create a new Python file in the appropriate adapter directory:

```
brad/adapters/<category>/my_new_adapter.py
```

### Step 2: Subclass the abstract base

```python
from brad.adapters.<category>.base import <BaseClass>
from brad.logging_config import get_logger


class MyNewAdapter(<BaseClass>):
    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        # Initialize with config values
        ...

    # Implement all abstract methods
    ...
```

### Step 3: Implement all abstract methods

Check the base class in `brad/adapters/<category>/base.py` for the full list of required methods. Every `@abstractmethod` must be implemented.

### Step 4: Register in configuration

Add any new config fields to `brad/config.py` and update `.env.example` with the new environment variables.

### Step 5: Wire into the orchestrator

Update `brad/orchestrator.py` to instantiate your adapter based on configuration. For example:

```python
if cfg.ticketing_provider == "linear":
    self.ticketing = LinearAdapter(cfg)
else:
    self.ticketing = JiraAdapter(cfg)
```

### Step 6: Write tests

Create `tests/test_my_new_adapter.py` with unit tests that mock external API calls.

### Step 7: Update documentation

- Add your adapter to the table in this file
- Update `README.md` if needed
- Update `.env.example` with any new environment variables

## Example: Adding a GitLab Adapter

```python
# brad/adapters/code_repository/gitlab_adapter.py
from brad.adapters.code_repository.base import CodeRepositoryAdapter
from brad.logging_config import get_logger


class GitLabAdapter(CodeRepositoryAdapter):
    def __init__(self, cfg):
        self.logger = get_logger(__name__)
        self.token = cfg.gitlab_token
        self.project_id = cfg.gitlab_project_id
        self.base_url = cfg.gitlab_url
        ...

    def open_pr(self, branch, title, body, base="main"):
        # GitLab calls these "Merge Requests"
        ...

    def get_pr(self, pr_number):
        ...

    # ... implement all other abstract methods
```

## Code Style

- Follow existing code patterns
- Use `brad.logging_config.get_logger(__name__)` for logging
- Type hints on all public methods
- Docstrings on classes and public methods

## Testing

Install dependencies with:

```bash
uv sync --extra test
```

Run the test suite:

```bash
uv run pytest tests/ -v
```

All tests must pass before submitting a PR.

## Database

Brad uses SQLite (`brad_data.db`) for execution history and cost tracking. The schema is managed in `brad/db.py`. If your changes require schema updates, add migration logic to `init_db()`.
