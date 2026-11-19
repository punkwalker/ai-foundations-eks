# Security

## Report a security issue

If you find a security issue in this project, report it through the [AWS vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/). You can also email aws-security@amazon.com.

Do not open a public GitHub issue for a security problem.

## Security checks in this repository

Every push runs the Automated Security Helper (ASH) workflow in `.github/workflows/ash.yml`. The workflow fails on any finding. Contributors run ASH on their own machine before they push, and they fix each finding. Nobody suppresses, baselines, or ignores a finding without written approval from the repository owner.
