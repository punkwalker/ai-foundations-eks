# Contributing

This repository holds the infrastructure for the AI Foundations on Amazon EKS workshop. The workshop content lives in a separate Workshop Studio repository.

## Branches

- Work on `dev`, or on a `feat/<stage>-<topic>` branch made from `dev`.
- Do not commit to `main`. Only the repository owner merges to `main`.

## Commits

- Make one logical change in each commit.
- Write the subject as `<area>: <what>`, in 72 characters or fewer.
- In the body, say why you made the change and how you checked it.
- Do not mix a refactor and a feature in one commit.
- Do not commit generated files or vendored code.

## Set up tools

Install [mise](https://mise.jdx.dev/), then run `mise install` from the repository root. It installs every tool at the version pinned in `mise.toml` and checked against `mise.lock`. Do not install tools with apt, brew, or a manual download.

To add or update a tool, edit `mise.toml`, then run `mise lock --platform linux-x64,linux-arm64,macos-arm64` and commit both files.

## Before you push

Run these commands from the repository root:

```
task lint
task test
task scan
```

`task scan` runs ASH. Fix every finding before you push. If you think a finding is wrong, raise it with the owner. Do not suppress it.

## Code rules

- Do not depend on internal-only packages. This repository must stay ready to become public.
- Write provisioning logic in Python, in the `aif` package. The `Taskfile.yaml` runs one command for each task.
- Do not add shell scripts. The IDE first-boot script, at most 10 lines, lives in the launch template in the content repository.
- Call AWS with boto3 and Kubernetes with the Kubernetes Python client. Do not call the `aws` or `kubectl` command line tools from Python.
- Pin every container image by digest, and every Helm chart and Git ref by version.
- Pin every GitHub Action by its full commit SHA.

## Cost

Add a row to `cost/cost-sheet.csv` for each billable AWS resource you add. Mark a price `verify` until you check it with the AWS Price List API.
