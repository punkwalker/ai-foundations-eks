# Decisions

These are the owner's design decisions as of November 18, 2026. The repo split revises D3 and D4.

| # | Decision |
|---|---|
| D1 | The content repo pushes each branch to Workshop Studio and to the owner's GitLab project, after ASH is clean. |
| D2 | This repo is private until the owner makes it public. Work happens on `dev`, and only the owner touches `main`. |
| D3 | CloudFormation creates only the IDE and its VPC. The IDE builds everything else. |
| D4 | Lab infra uses the internal workshop CDK constructs. Task R1 reviews them and picks the language before W2 starts. |
| D5 | Provisioning code is the typed Python tool `aif`. The Taskfile runs one command per task, and the repo has no shell scripts. |
| D6 | The IDE and Hub share one VPC. Sharing it with Dev and Prod is a parameter the owner confirms at the W2 review. |
| D7 | Slow parts are built at provisioning. The lab applies only RGDs and apps. |
| D8 | Kargo handles promotion. Dapr and Temporal both stay as options for durable agents. Nightly runs come later. |
| D9 | Content stays in the owner's GitLab for now. A move to a shared team group comes later. |
| D3 revised | The launch CloudFormation creates the IDE, its VPC, GitLab behind CloudFront, and the IAM Identity Center instance. It lives in the content repo. This repo holds only what the IDE brings up, and only configures Identity Center groups and Argo CD role mappings. |
| D4 revised | The content repo builds the launch template from the internal workshop CDK constructs. This repo depends on no internal-only package, and uses its own TypeScript CDK only if needed. |
| D10 | Stages form groups. Only Base is shared, and groups B to E do not depend on each other. |
| D11 | Tools are managed by mise (mise.toml); no apt/brew install steps anywhere |

Table note: thirteen rows (count) as of November 19, 2026, one decision per row. The two revised rows come from the repo split and replace D3 and D4 where they differ. D2 also covers the GitLab mirror on the IDE. Source is the owner's design decisions. The owner added D11 in chat on November 19, 2026.

## IDE first-boot script (for the content worker)

The launch template in the content repo owns this script. Under D11 it installs mise, installs the tools from `mise.toml`, and runs `task up`. It uses 5 of the 10 allowed lines:

```
curl -fsSLo /usr/local/bin/mise https://github.com/jdx/mise/releases/download/v2026.11.10/mise-v2026.11.10-linux-x64
sha256sum -c <(printf '%s  %s\n' b603a4f48563fc379a794d9ea2cf9f709ed276c9141068604cb0c59fa3e674a9 /usr/local/bin/mise) && chmod 0755 /usr/local/bin/mise
git clone --branch dev https://github.com/punkwalker/ai-foundations-eks.git /home/participant/ai-foundations-eks
cd /home/participant/ai-foundations-eks && mise trust && mise install --locked
mise exec -- task up
```

The checksum is for the `linux-x64` build of mise 2026.11.10. An arm64 IDE needs the `linux-arm64` file and checksum `86ef275d541c5243cd20b1c0fcd16810e5f3601113d8db09278d0608617a3833`. The clone works without a credential only after the owner makes this repo public. The user name `participant` and the paths are placeholders for the content worker to match to the IDE.

## ASH is pinned by tag

`mise.toml` pins Automated Security Helper (ASH) by the GitHub tag `v3.8.0`. A tag can move. The upstream maintainers can point it at a different commit. Our next install would then pick that commit up with no change in this repo. The owner accepted this risk on November 19, 2026, because the other pins fail:

1. A full commit hash in `mise.toml` trips the ASH detect-secrets scanner (SECRET-HEX-HIGH-ENTROPY-STRING). The repo rule bans suppressing that finding.
2. ASH is not on PyPI. The PyPI name `automated-security-helper` belongs to an unrelated one-release placeholder (11.0.2, summary "test-package-placeholder", no project links). Never install ASH from PyPI.

On November 19, 2026, the tag `v3.8.0` pointed to commit `f356ca7`. Recheck it when ASH is upgraded.
