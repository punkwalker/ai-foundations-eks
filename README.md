# AI Foundations on Amazon EKS: infrastructure

This repository builds the lab environment for the AI Foundations on Amazon EKS workshop. The workshop content lives in a separate Workshop Studio repository.

The launch CloudFormation template lives in the content repository. It creates the IDE, its VPC, GitLab behind CloudFront, and IAM Identity Center.

## Start-up sequence

1. The launch template from the content repository creates the IDE instance and its VPC.
2. On first boot, the IDE installs uv, clones this repository, and runs `task up`.
3. `task up` runs the `aif` command line tool, which builds the rest of the lab from the IDE.

## Layout

| Folder | Contents |
|---|---|
| `aif/` | Python command line tool that provisions and checks the lab |
| `infra/` | Our own CDK app in TypeScript, only if needed |
| `platform/` | Argo CD ApplicationSets, kro RGDs, addon values, and Kyverno policies |
| `stages/` | One folder for each of the 14 workshop stages |
| `workloads/` | Sample agents, MCP servers, and skills |
| `tests/` | Repository-wide tests |
| `cost/` | Cost sheet for every billable resource |
| `docs/` | Design decisions |

Table note: eight top-level folders (count) in this repository as of November 19, 2026, one per row. Each folder except `docs/` has its own README.

## Common tasks

| Command | Action |
|---|---|
| `task up` | Build the lab |
| `task down` | Remove the lab |
| `task status` | Show the state of each stage |
| `task doctor` | Check local tools and access |
| `task lint` | Run linters and type checks |
| `task test` | Run unit tests |
| `task scan` | Run ASH |

Table note: seven tasks (count) in `Taskfile.yaml` as of November 19, 2026, one per row. Each task runs one command. The first four print "not implemented" until the `aif` commands exist.

## Contributing and security

See `CONTRIBUTING.md` and `SECURITY.md`. This project uses the MIT-0 license, in `LICENSE`.
