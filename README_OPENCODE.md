# OpenCode CLI Integration for Brad

Brad now supports both **Claude Code** and **OpenCode CLI** as AI coding agents!

## Configuration

In your `.env` file, set the `AI_AGENT` variable to choose which agent to use:

```bash
# Options: "claude" or "opencode"
AI_AGENT=opencode
```

### Claude Code Configuration
```bash
AI_AGENT=claude
CLAUDE_CLI_PATH=claude
```

### OpenCode CLI Configuration
```bash
AI_AGENT=opencode
OPENCODE_CLI_PATH=opencode
```

## How It Works

Brad uses a unified AI agent interface that abstracts the differences between Claude Code and OpenCode CLI. Both agents:

- Receive the same prompts for requirements analysis, implementation, and CI fixes
- Return structured JSON responses
- Have full git access for commits and pushes
- Can create pull requests

## Switching Between Agents

Simply update the `AI_AGENT` setting in your `.env` file and restart Brad. No code changes needed!

## OpenCode CLI Features

OpenCode CLI provides:
- **Multiple AI providers**: Support for various AI models
- **Session management**: Continue previous sessions
- **GitHub integration**: Built-in PR handling
- **Web interface**: Optional web UI via `opencode web`
- **Statistics tracking**: Token usage and costs

## Testing the Integration

Run the test script to verify everything is configured correctly:

```bash
python test_opencode.py
```

This will verify:
- Configuration loads correctly
- AI agent interface initializes
- OpenCode CLI is accessible
- Prompts are built correctly

## Command Reference

Useful OpenCode CLI commands:

```bash
# Start OpenCode in a project
opencode /path/to/project

# Run with a specific message
opencode run /path/to/project "Your task here"

# Continue last session
opencode --continue /path/to/project

# View help
opencode --help
```

## Architecture

```
brad_orchestrator.py
    ↓
create_ai_agent_interface(cfg)
    ↓
    ├─→ ClaudeCodeInterface (if AI_AGENT=claude)
    └─→ OpenCodeInterface (if AI_AGENT=opencode)
```

Both interfaces implement the same abstract methods:
- `invoke_requirements_analysis()`
- `invoke_implementation()`
- `invoke_ci_fix()`

## Troubleshooting

**OpenCode not found:**
- Ensure OpenCode CLI is installed: Check with `opencode --help`
- Verify the path in `OPENCODE_CLI_PATH`

**Authentication issues:**
- Run `opencode auth` to configure credentials
- Ensure you have access to the required AI providers

**Session issues:**
- Use `opencode session` to manage sessions
- Check session logs with `opencode debug`

## Example Workflow

1. Create JIRA issue with requirements
2. Add `BradReview` label
3. Brad picks up the issue
4. OpenCode CLI analyzes requirements
5. OpenCode CLI implements the feature
6. OpenCode CLI creates PR
7. Brad monitors CI/CD
8. Brad updates JIRA status

All orchestrated automatically by Brad!
