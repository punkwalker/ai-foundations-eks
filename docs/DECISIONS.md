# Decisions

These are the owner's design decisions as of November 18, 2026.

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
| D10 | Stages form groups. Only Base is shared, and groups B to E do not depend on each other. |

Table note: ten decisions (count) in force as of November 18, 2026, one per row. D2 also covers the GitLab mirror on the IDE. Source is the owner's design decisions.
