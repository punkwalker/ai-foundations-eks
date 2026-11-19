# CloudFormation

This folder holds one template. It creates the IDE instance, its VPC, and the GitLab mirror behind CloudFront.

The IDE first-boot script lives in the template and has at most 10 lines. It installs uv, clones this repository, and runs `task up`. The `aif` tool builds all other lab infrastructure from the IDE.
