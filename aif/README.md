# aif

This folder holds `aif`, the Python command line tool that provisions and checks the workshop lab. It calls AWS through boto3 and Kubernetes through the Kubernetes Python client.

Every command is a stub today. Each one prints "not implemented" and exits with code 2. Only `aif --version` works.

## Commands

| Command | Action |
|---|---|
| `aif up` | Build the lab |
| `aif down` | Remove the lab |
| `aif status` | Show the state of each stage |
| `aif doctor` | Check local tools and access |
| `aif stage on NN` or `aif stage off NN` | Turn stage NN on or off |
| `aif cleanup --report` | List leftover resources |
| `aif cost` | Show the cost of running resources |

Table note: seven commands (count) as of November 19, 2026, one per row. All seven are stubs.

## Develop

Run these commands from this folder:

```
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
```
