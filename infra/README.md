# Infra

This folder holds our own CDK app, in TypeScript, for lab infrastructure that the IDE brings up. Add it only if `aif` cannot do the job with boto3 alone. Until then, the folder stays empty.

Do not depend on internal-only packages here. This repository must stay ready to become public. The launch template built from the internal workshop CDK constructs lives in the content repository.
