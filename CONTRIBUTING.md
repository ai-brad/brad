# Contributing to Brad

Thank you for your interest in contributing to Brad! This guide covers how to set up your development environment, add new adapters, and contribute code.

## Table of Contents

- [Development Setup](#development-setup)
- [Running Tests](#running-tests)
- [Code Style](#code-style)
- [Adding New Adapters](#adding-new-adapters)
- [Pull Request Process](#pull-request-process)
- [Commit Guidelines](#commit-guidelines)
- [Release Process](#release-process)

## Development Setup

### Prerequisites

- Python 3.12 or higher
- Git
- uv (Python package manager)

### Initial Setup

1. **Fork the repository** on GitHub

2. **Clone your fork:**
```bash
git clone https://github.com/YOUR_USERNAME/brad.git
cd brad
```

3. **Add upstream remote:**
```bash
git remote add upstream https://github.com/original/brad.git
```

4. **Install uv** (if not already installed):
```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
irm https://astral.sh/uv/install.ps1 | iex
```

5. **Install dependencies:**
```bash
# Install main dependencies
uv sync

# Install test dependencies
uv sync --extra test
```

6. **Set up configuration:**
```bash
cp .env.example .env
# Edit .env with your credentials
```

7. **Verify installation:**
```bash
uv run python brad.py --help
```

### Development Workflow

1. **Create a feature branch:**
```bash
git checkout -b feature/your-feature-name
```

2. **Make your changes**

3. **Run tests:**
```bash
uv run pytest tests/ -v
```

4. **Check code style:**
```bash
uv run black brad/ tests/
uv run flake8 brad/ tests/
uv run mypy brad/
```

5. **Commit and push:**
```bash
git add .
git commit -m "feat: your feature description"
git push origin feature/your-feature-name
```

6. **Open a Pull Request** on GitHub

## Running Tests

### Run All Tests

```bash
uv run pytest tests/ -v
```

### Run Specific Test File

```bash
uv run pytest tests/test_orchestrator.py -v
```

### Run Tests with Coverage

```bash
uv run pytest tests/ --cov=brad --cov-report=term-missing
```

### Generate HTML Coverage Report

```bash
uv run pytest tests/ --cov=brad --cov-report=html
# Open htmlcov/index.html in your browser
```

### Run Tests in Watch Mode

```bash
uv run pytest-watch tests/
```

### Test Guidelines

- All new features must include tests
- Aim for >80% code coverage on new code
- Use pytest fixtures for common test setup
- Mock external API calls (don't make real API requests in tests)
- Test both success and failure cases
- Keep tests fast (<5 seconds for the full suite)

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

### Automated Formatting

We use **Black** for code formatting:

```bash
# Format all code
uv run black brad/ tests/

# Check formatting without making changes
uv run black --check brad/ tests/
```

### Linting

We use **Flake8** for linting:

```bash
uv run flake8 brad/ tests/
```

### Type Checking

We use **MyPy** for static type checking:

```bash
uv run mypy brad/
```

### Pre-commit Checks

Before committing, run all quality checks:

```bash
# Format code
uv run black brad/ tests/

# Check linting
uv run flake8 brad/ tests/

# Check types
uv run mypy brad/

# Run tests
uv run pytest tests/ -v
```

### Style Guidelines

- **Follow existing code patterns** - Look at similar files for consistency
- **Type hints everywhere** - All functions should have type annotations
- **Docstrings** - All public classes and methods need docstrings
- **Logging** - Use `brad.logging_config.get_logger(__name__)` for all logging
- **Error handling** - Use specific exceptions, not bare `except:`
- **Line length** - Max 100 characters (Black will handle this)
- **Imports** - Group stdlib, third-party, and local imports separately
- **Naming conventions:**
  - Classes: `PascalCase`
  - Functions/methods: `snake_case`
  - Constants: `UPPER_SNAKE_CASE`
  - Private methods: `_leading_underscore`

### Example Code

```python
from typing import Optional, List
from brad.logging_config import get_logger

logger = get_logger(__name__)


class MyAdapter:
    """Adapter for XYZ service.
    
    This adapter implements the AbstractAdapter interface for XYZ.
    """
    
    def __init__(self, api_key: str) -> None:
        """Initialize the adapter.
        
        Args:
            api_key: API key for XYZ service
        """
        self.api_key = api_key
        self.logger = get_logger(__name__)
    
    def fetch_data(self, resource_id: str) -> Optional[dict]:
        """Fetch data from XYZ service.
        
        Args:
            resource_id: The resource identifier
            
        Returns:
            Resource data if found, None otherwise
            
        Raises:
            APIError: If the API request fails
        """
        try:
            # Implementation here
            self.logger.info(f"Fetching resource {resource_id}")
            return {"id": resource_id}
        except Exception as e:
            self.logger.error(f"Failed to fetch {resource_id}: {e}")
            raise
```

## Adding New Adapters

See the [Architecture Overview](#architecture-overview) section below for detailed instructions on creating new adapters.

## Pull Request Process

### Before Submitting

1. **Update your branch** with the latest upstream:
```bash
git fetch upstream
git rebase upstream/main
```

2. **Run all quality checks:**
```bash
uv run black brad/ tests/
uv run flake8 brad/ tests/
uv run mypy brad/
uv run pytest tests/ -v
```

3. **Update documentation** if needed:
   - Update README.md for user-facing changes
   - Update CONTRIBUTING.md for developer-facing changes
   - Add docstrings to new code
   - Update .env.example if adding config options

4. **Add tests** for new features

5. **Update CHANGELOG.md** (if applicable)

### Submitting the PR

1. **Push to your fork:**
```bash
git push origin feature/your-feature-name
```

2. **Create Pull Request** on GitHub with:
   - Clear title following commit conventions (e.g., "feat: add GitLab adapter")
   - Description of what changed and why
   - Reference any related issues (e.g., "Closes #123")
   - Screenshots/GIFs for UI changes
   - Test results or coverage report

3. **Respond to review feedback** promptly

### PR Checklist

- [ ] Code follows the style guidelines
- [ ] All tests pass
- [ ] New code has tests
- [ ] Documentation updated
- [ ] CHANGELOG.md updated (for significant changes)
- [ ] Commit messages follow conventions
- [ ] No merge conflicts with main branch

## Commit Guidelines

We follow [Conventional Commits](https://www.conventionalcommits.org/):

### Format

```
<type>(<scope>): <subject>

<body>

<footer>
```

### Types

- **feat**: New feature
- **fix**: Bug fix
- **docs**: Documentation changes
- **style**: Code style changes (formatting, no logic change)
- **refactor**: Code refactoring
- **perf**: Performance improvements
- **test**: Adding or updating tests
- **chore**: Maintenance tasks
- **ci**: CI/CD changes

### Examples

```bash
# Feature
git commit -m "feat(adapters): add GitLab repository adapter"

# Bug fix
git commit -m "fix(orchestrator): handle missing PR number"

# Documentation
git commit -m "docs(readme): add troubleshooting section"

# Breaking change
git commit -m "feat(config)!: change env var naming convention

BREAKING CHANGE: All env vars now use BRAD_ prefix"
```

### Scope Guidelines

- `adapters`: Adapter-related changes
- `orchestrator`: Core orchestration logic
- `gui`: Web dashboard
- `db`: Database/persistence
- `config`: Configuration
- `tests`: Test-related changes
- `docs`: Documentation

## Release Process

### Version Numbering

We follow [Semantic Versioning](https://semver.org/):

- **MAJOR** (1.0.0): Breaking changes
- **MINOR** (0.1.0): New features, backwards compatible
- **PATCH** (0.0.1): Bug fixes, backwards compatible

### Creating a Release

1. **Update version** in `pyproject.toml`

2. **Update CHANGELOG.md** with release notes

3. **Commit changes:**
```bash
git commit -m "chore: bump version to 0.2.0"
```

4. **Create tag:**
```bash
git tag -a v0.2.0 -m "Release v0.2.0"
```

5. **Push to GitHub:**
```bash
git push origin main
git push origin v0.2.0
```

6. **Create GitHub Release** with release notes from CHANGELOG

7. **Publish to PyPI** (maintainers only):
```bash
uv build
uv publish
```

## Testing

See the [Running Tests](#running-tests) section above for detailed testing instructions.

## Database Migrations

Brad uses SQLite (`brad_data.db`) for execution history and cost tracking. 

### Schema Changes

If your changes require schema updates:

1. Create a new migration file in `brad/migrations/`:
   - Use sequential numbering: `003_your_change.sql`
   - Include only DDL statements (CREATE, ALTER, etc.)

2. Migrations run automatically on startup via `brad/db.py`

3. Test migrations on a copy of production data

### Example Migration

```sql
-- brad/migrations/003_add_retry_count.sql
ALTER TABLE executions ADD COLUMN retry_count INTEGER DEFAULT 0;
CREATE INDEX idx_retry_count ON executions(retry_count);
```

## Need Help?

- 📖 Read the [README](README.md) for user documentation
- 💬 Ask questions in [GitHub Discussions](https://github.com/yourusername/brad/discussions)
- 🐛 Report bugs in [Issues](https://github.com/yourusername/brad/issues)
- 📧 Email maintainers at dev@brad-project.dev

## Code of Conduct

Please read and follow our [Code of Conduct](CODE_OF_CONDUCT.md).

Thank you for contributing to Brad! 🎉
