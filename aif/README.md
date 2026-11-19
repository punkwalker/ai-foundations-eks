# aif

This folder holds `aif`, the Python command line tool that provisions, checks, and removes the workshop lab. It calls AWS through boto3 and Kubernetes through the Kubernetes Python client. It runs no shell commands.

## Commands

| Command | Action |
|---|---|
| `aif up --confirm-account ID` | Print the bring-up plan. Add `--apply` to turn stages on and wait for them |
| `aif down --confirm-account ID` | Print the teardown plan. Add `--apply` to remove the lab |
| `aif status` | Show each stage, its label state, and its Applications that are not ready |
| `aif doctor` | Show the Argo CD mode, sign-in method, AWS identity, and unhealthy Applications |
| `aif stage on NN` or `aif stage off NN` | Turn a stage on with its needs, or off with its dependents |
| `aif stage list` or `aif stage graph` | List the stages, or show what each stage needs |
| `aif cleanup --report` | List what `aif down` would remove, and delete nothing |
| `aif cost` | Sum `cost/cost-sheet.csv` for each stage |

Table note: eight commands (count) as of November 19, 2026, one per row. `up` and `down` stop with exit code 3 when `--confirm-account` differs from the account of the current AWS credentials.

`aif down --apply` takes two more flags. `--force-finalizers` removes stuck finalizers and logs each one. `--include-untagged` also deletes resources that lack the `project` tag.

## Settings

aif reads its settings from `aif.toml` in the repository root, or from the file in `AIF_CONFIG`. Environment variables override the file.

| Setting | Variable | Default |
|---|---|---|
| `region` | `AIF_REGION` | `us-west-2` |
| `groups` | `AIF_GROUPS` | all groups |
| `argocd_mode` | `AIF_ARGOCD_MODE` | `capability` |
| `argocd_namespace` | `AIF_ARGOCD_NAMESPACE` | `argocd` |
| `hub_cluster` | `AIF_HUB_CLUSTER` | `hub` |
| `contexts.hub`, `.dev`, `.prod` | `AIF_HUB_CONTEXT`, `AIF_DEV_CONTEXT`, `AIF_PROD_CONTEXT` | `hub`, `dev`, `prod` |
| `keep_stacks` | `AIF_KEEP_STACKS` | none |
| `project_tags` | none | `project=ai-foundations-eks`, `owner=punkwalker` |

Table note: eight settings (count) as of November 19, 2026, one per row. Lists in variables are comma separated. `hub_cluster` names the Argo CD cluster Secret for the Hub. `keep_stacks` names the CloudFormation stacks that `aif down` keeps, such as the launch stack.

## Modules

| Module | Job |
|---|---|
| `config` | Typed settings |
| `stages` | Load and check `stage.yaml` files, and order stages |
| `kube` | Small Kubernetes interface and its client implementation |
| `waiting` | Waits with a timeout and backoff |
| `argocd` | List, refresh, and wait for Applications |
| `labels` | Stage and group labels on the Hub cluster Secret |
| `bringup` | Turn stages on in order |
| `inventory` | Find what to remove, from four sources |
| `predelete` | Empty buckets, delete repositories, secrets, roles, and VPC blockers |
| `teardown` | Remove the lab in reverse order |
| `doctor` | Report mode and health |
| `aws` | Account checks and boto3 clients |
| `cost` | Sum the cost sheet |
| `cli` | Command line |

Table note: fourteen modules (count) in `src/aif/` as of November 19, 2026, one per row.

## Develop

Run these commands from this folder:

```
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
```

The tests use fakes for Kubernetes and botocore Stubber for AWS. They make no network calls.
