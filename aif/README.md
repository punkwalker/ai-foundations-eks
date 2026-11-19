# aif

This folder holds `aif`, the Python command line tool that provisions, checks, and removes the workshop lab. It calls AWS through boto3 and Kubernetes through the Kubernetes Python client. It starts no other programs.

## Commands

| Command | Action |
|---|---|
| `aif up` | Print the bring-up plan. Add `--apply` to build the Hub, its capabilities, and every stage |
| `aif down --confirm-account ID` | Print the teardown plan. Add `--apply` to remove the lab |
| `aif dev network up` or `down` | In a development account, create or remove a stand-in for the launch stack |
| `aif status` | Show each stage, its label state, and its Applications that are not ready |
| `aif doctor` | Show the Argo CD mode, sign-in method, AWS identity, and unhealthy Applications |
| `aif stage on NN` or `aif stage off NN` | Turn a stage on with its needs, or off with its dependents |
| `aif stage list` or `aif stage graph` | List the stages, or show what each stage needs |
| `aif cleanup --report` | List what `aif down` would remove, and delete nothing |
| `aif cost` | Sum `cost/cost-sheet.csv` for each stage |

Table note: nine commands (count) as of November 19, 2026, one per row. `up` takes the account id from `--confirm-account` or from `AIF_CONFIRM_ACCOUNT`. `down` and `dev network` take it only from `--confirm-account`. Each stops with exit code 3 when the id differs from the account of the current AWS credentials.

`aif down --apply` takes two more flags. `--force-finalizers` removes stuck finalizers and logs each one. `--include-untagged` also deletes resources that lack the `project` tag.

## Bring-up order

`aif up --apply` runs these steps. Each wait has a time limit and says what was not ready.

1. Read the launch inputs from SSM under `/aif/`.
2. Create the shared platform (boundary, bucket, ECR, KMS key, secrets) and start the Hub cluster `aif-hub`.
3. Wait up to 30 minutes for the Hub, then write the `aif-hub` kubeconfig context.
4. Start the kro and ACK capabilities, and Argo CD in `capability` mode. Wait up to 30 minutes for each to be ACTIVE.
5. Wait for the Argo CD API, then write the Hub cluster Secret and the group labels.
6. Argo CD syncs the platform. Its kro `PlatformCluster` RGD creates `aif-dev` and `aif-prod` through ACK. Wait up to 45 minutes for both clusters and their Argo CD cluster Secrets, labeled `platform.aws/cluster: dev` and `prod`.
7. Write the `aif-dev` and `aif-prod` contexts, then turn the stages on in order.

The kubeconfig goes to the first file in `KUBECONFIG`, or `~/.kube/config`. Each user entry runs `aws eks get-token`, so the file holds no token.

## Teardown order

`aif down --apply` removes the lab in reverse order:

1. Stage labels off, then Applications, with their cascade finalizer.
2. kro instances. `PlatformCluster` removes Dev and Prod through ACK.
3. ACK resources, then the pre-delete steps.
4. Hub capabilities, the Hub cluster, and the platform.
5. A final tag sweep, which must find nothing.

## Stacks and resources that `aif down` keeps

`aif down` never deletes these stacks or the resources in them:

1. The launch stack. Its name is in the SSM parameter `/aif/launch-stack-name`. If aif cannot read that parameter, `aif down` refuses to run.
2. Any stack or resource tagged `aif:keep=true`.
3. Any stack named in the `keep_stacks` setting.

## Settings

aif reads its settings from `aif.toml` in the repository root, or from the file in `AIF_CONFIG`. Environment variables override the file.

| Setting | Variable | Default |
|---|---|---|
| `region` | `AIF_REGION` | `us-west-2` |
| `groups` | `AIF_GROUPS` | all groups |
| `argocd_mode` | `AIF_ARGOCD_MODE` | `capability` |
| `argocd_namespace` | `AIF_ARGOCD_NAMESPACE` | `argocd` |
| `hub_cluster` | `AIF_HUB_CLUSTER` | `hub` |
| `contexts.hub`, `.dev`, `.prod` | `AIF_HUB_CONTEXT`, `AIF_DEV_CONTEXT`, `AIF_PROD_CONTEXT` | `aif-hub`, `aif-dev`, `aif-prod` |
| `keep_stacks` | `AIF_KEEP_STACKS` | none |
| `launch_stack_parameter` | `AIF_LAUNCH_STACK_PARAMETER` | `/aif/launch-stack-name` |
| `project_tags` | `AIF_OWNER_TAG` adds `owner` | `project=ai-foundations-eks` |

Table note: nine settings (count) as of November 19, 2026, one per row. Lists in variables are comma separated. `hub_cluster` names the Argo CD cluster Secret for the Hub.

## Modules

| Module | Job |
|---|---|
| `config` | Typed settings |
| `launch` | Read the launch inputs from SSM |
| `hub_api` | Interface to the Hub code: `HubApi`, `DevNetworkApi`, and their loaders |
| `hub` | Create and remove the Hub with boto3 (implements `hub_api`) |
| `up` | The `aif up` sequence and its waits |
| `stages` | Load and check `stage.yaml` files, and order stages |
| `kube` | Small Kubernetes interface and its client implementation |
| `eks` | Kubeconfig contexts and Argo CD cluster Secrets from EKS data |
| `waiting` | Waits with a timeout and backoff |
| `argocd` | List, refresh, and wait for Applications |
| `labels` | Stage and group labels on the Hub cluster Secret |
| `bringup` | Turn stages on in order |
| `keep` | Find the stacks that `aif down` keeps |
| `inventory` | Find what to remove, from four sources |
| `predelete` | Empty buckets, delete repositories, secrets, roles, and VPC blockers |
| `teardown` | Remove the lab in reverse order |
| `doctor` | Report mode and health |
| `aws` | Account checks and boto3 clients |
| `cost` | Sum the cost sheet |
| `cli` | Command line |

Table note: twenty modules (count) in `src/aif/` as of November 19, 2026, one per row. `hub` is written separately against `hub_api`. Until it exists, `aif up --apply` and `aif down --apply` stop with "aif.hub is not built yet".

## Develop

Run these commands from this folder:

```
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
```

The tests use fakes for Kubernetes and the Hub, and botocore Stubber for AWS. They make no network calls. A test fails if any module imports `subprocess`.
