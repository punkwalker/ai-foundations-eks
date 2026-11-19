"""Remove the lab, stage by stage, in reverse bring-up order.

For each stage, last stage first:

1. Turn the stage label off, so Argo CD stops creating its Applications.
2. Prepare the stage's tagged AWS resources: empty S3 buckets, force-delete ECR
   repositories, delete secrets with no recovery window. This runs before the
   Kubernetes deletes, because ACK cannot delete a bucket that still has objects.
3. Delete the stage's Applications and wait until they are gone.
4. Delete the stage's kro instances and wait, then its ACK resources and wait.

Then, once for the whole lab:

5. Delete agent IAM roles under `/aif/agents/`.
6. Clear load balancers and free network interfaces that block VPC deletion.
7. Delete the remaining CloudFormation stacks tagged with the project, except kept stacks.
8. Sweep by tag again. The teardown succeeds only if the sweep finds nothing.

Removing finalizers by force needs `force_finalizers=True`, and each one is logged.
"""

# Lets the dataclass below use the boto3 client types, which exist only for type checking.
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aif import predelete
from aif.argocd import missing_cascade_finalizer
from aif.inventory import Inventory, Item, tag_sweep
from aif.kube import KubeApi, KubeError, Resource
from aif.labels import off_labels, read_labels, set_labels
from aif.stages import Stage
from aif.waiting import Clock, WaitTimeoutError, until

if TYPE_CHECKING:
    from mypy_boto3_cloudformation import CloudFormationClient
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_ecr import ECRClient
    from mypy_boto3_elbv2 import ElasticLoadBalancingv2Client
    from mypy_boto3_iam import IAMClient
    from mypy_boto3_resourcegroupstaggingapi import ResourceGroupsTaggingAPIClient
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_secretsmanager import SecretsManagerClient

Log = Callable[[str], None]


@dataclass(frozen=True)
class AwsClients:
    s3: S3Client
    ecr: ECRClient
    secrets: SecretsManagerClient
    ec2: EC2Client
    elbv2: ElasticLoadBalancingv2Client
    iam: IAMClient
    tagging: ResourceGroupsTaggingAPIClient
    cloudformation: CloudFormationClient


@dataclass(frozen=True)
class Options:
    project: str
    argocd_namespace: str
    hub_cluster: str
    keep_stacks: tuple[str, ...] = ()
    include_untagged: bool = False
    force_finalizers: bool = False
    stage_timeout: float = 900.0
    stack_timeout: float = 1800.0


@dataclass
class Result:
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    leftovers: list[Item] = field(default_factory=list)

    def fail(self, problem: str) -> None:
        self.ok = False
        self.problems.append(problem)


@dataclass
class Teardown:
    stages: Mapping[str, Stage]
    clusters: Mapping[str, KubeApi]
    aws: AwsClients
    opts: Options
    log: Log = print
    clock: Clock = field(default_factory=Clock)

    def run(self, inventory: Inventory, stage_order: Sequence[str]) -> Result:
        result = Result()
        for stage_id, items in inventory.by_stage(stage_order):
            if not self._stage(stage_id, items, result):
                return result
        self._account_wide(inventory, result)
        if result.ok:
            self._final_sweep(result)
        return result

    # One stage

    def _stage(self, stage_id: str, items: list[Item], result: Result) -> bool:
        self.log(f"stage {stage_id}: {len(items)} items")
        if stage_id in self.stages:
            self._label_off(stage_id)
        for item in items:
            if item.source == "aws":
                self._prepare(item, result)
        for source in ("argocd", "kro", "ack"):
            batch = [i for i in items if i.source == source]
            if batch and not self._delete_and_wait(stage_id, batch, result):
                return False
        return True

    def _label_off(self, stage_id: str) -> None:
        hub = self.clusters["hub"]
        ns, secret = self.opts.argocd_namespace, self.opts.hub_cluster
        current = read_labels(hub, ns, secret)
        changed = set_labels(hub, ns, secret, off_labels(self.stages, stage_id, current))
        self.log(f"  labels off: {sorted(changed) or 'already off'}")

    def _prepare(self, item: Item, result: Result) -> None:
        if item.kind == "s3":
            done = predelete.empty_bucket(self.aws.s3, item.name)
        elif item.kind == "ecr/repository":
            done = predelete.force_delete_ecr_repository(self.aws.ecr, item.arn.split("/", 1)[1])
        elif item.kind == "secretsmanager":
            done = predelete.delete_secret_now(self.aws.secrets, item.arn)
        else:
            return
        self.log(f"  {done.step}: {', '.join(done.done)}")

    def _delete_and_wait(self, stage_id: str, batch: list[Item], result: Result) -> bool:
        source = batch[0].source
        if source == "argocd":
            self._warn_missing_finalizers(batch)
        for item in batch:
            self._api(item).delete_object(_res(item), item.name, item.namespace)
        try:
            self._wait_gone(batch, f"stage {stage_id} {source} objects")
            return True
        except WaitTimeoutError as err:
            if not self.opts.force_finalizers:
                result.fail(f"{err}. Fix the cause, or rerun with --force-finalizers")
                return False
            self._force_finalizers(batch)
            try:
                self._wait_gone(batch, f"stage {stage_id} {source} objects after force")
                return True
            except WaitTimeoutError as err2:
                result.fail(str(err2))
                return False

    def _warn_missing_finalizers(self, batch: Iterable[Item]) -> None:
        hub = self.clusters["hub"]
        apps = [o for i in batch if (o := hub.get_object(_res(i), i.name, i.namespace))]
        for name in missing_cascade_finalizer(apps):
            self.log(f"  WARNING: Application {name} has no cascade finalizer; resources stay")

    def _remaining(self, batch: Iterable[Item]) -> list[Item]:
        left = []
        for item in batch:
            try:
                if self._api(item).get_object(_res(item), item.name, item.namespace) is not None:
                    left.append(item)
            except KubeError as err:
                if err.status != 404:
                    raise
        return left

    def _wait_gone(self, batch: list[Item], what: str) -> None:
        def check() -> tuple[bool, list[Item]]:
            left = self._remaining(batch)
            return not left, left

        until(
            check,
            timeout=self.opts.stage_timeout,
            what=what,
            describe=lambda left: "still present: " + ", ".join(str(i) for i in left),
            clock=self.clock,
        )

    def _force_finalizers(self, batch: list[Item]) -> None:
        for item in self._remaining(batch):
            self.log(f"  !!! FORCING FINALIZERS OFF {item} !!! cloud resources may be left behind")
            body = {"metadata": {"finalizers": None}}
            self._api(item).patch_object(_res(item), item.name, body, item.namespace)

    def _api(self, item: Item) -> KubeApi:
        return self.clusters[item.where]

    # Whole account

    def _account_wide(self, inventory: Inventory, result: Result) -> None:
        roles = predelete.delete_agent_roles(
            self.aws.iam, self.opts.project, self.opts.include_untagged
        )
        self._report(roles)
        for vpc in [i for i in inventory.items if i.kind == "ec2/vpc"]:
            blockers = predelete.vpc_blockers(
                self.aws.ec2, self.aws.elbv2, vpc.name.split("/", 1)[-1], self.opts.project
            )
            self._report(
                predelete.clear_vpc_blockers(
                    self.aws.ec2, self.aws.elbv2, blockers, self.opts.include_untagged
                )
            )
        stacks = [i.arn for i in inventory.items if i.kind == "cloudformation/stack"]
        for problem in self._delete_stacks(stacks):
            result.fail(problem)

    def _report(self, step: predelete.StepResult) -> None:
        self.log(f"{step.step}: {len(step.done)} done")
        for skipped in step.skipped:
            self.log(f"  skipped {skipped}")

    def _delete_stacks(self, stack_arns: list[str]) -> list[str]:
        cfn = self.aws.cloudformation
        for arn in stack_arns:
            self.log(f"delete stack {arn}")
            cfn.delete_stack(StackName=arn)

        def check() -> tuple[bool, dict[str, str]]:
            states = {}
            for arn in stack_arns:
                stack = cfn.describe_stacks(StackName=arn)["Stacks"][0]
                if stack["StackStatus"] != "DELETE_COMPLETE":
                    states[arn] = f"{stack['StackStatus']} {stack.get('StackStatusReason', '')}"
            failed = any("DELETE_FAILED" in s for s in states.values())
            return not states or failed, states

        if not stack_arns:
            return []
        try:
            states = until(
                check,
                timeout=self.opts.stack_timeout,
                what="CloudFormation stacks to delete",
                describe=lambda s: "; ".join(f"{k}: {v}" for k, v in s.items()),
                interval=15.0,
                clock=self.clock,
            )
        except WaitTimeoutError as err:
            return [str(err)]
        return [f"stack {arn}: {state}" for arn, state in states.items()]

    def _final_sweep(self, result: Result) -> None:
        left = tag_sweep(self.aws.tagging, self.opts.project, self.opts.keep_stacks)
        if left:
            result.leftovers = left
            result.fail(f"final tag sweep found {len(left)} resources tagged project")
        else:
            self.log("final tag sweep: zero resources tagged project")


def _res(item: Item) -> Resource:
    if item.resource is None:
        raise ValueError(f"{item} has no Kubernetes resource type")
    return item.resource
