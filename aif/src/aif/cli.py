"""Command line entry point for aif.

`up`, `down` and `dev network` change AWS. Each refuses to run unless the
confirmed account matches the account of the current AWS credentials, and each
only prints its plan unless `--apply` is given. `up` takes the account from `--confirm-account`
or `AIF_CONFIRM_ACCOUNT`. The others take it only from `--confirm-account`.
"""

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError

from aif import (
    __version__,
    aws,
    bringup,
    config,
    cost,
    doctor,
    eks,
    hub_api,
    inventory,
    keep,
    labels,
    launch,
    stages,
    up,
)
from aif.argocd import STAGE_LABEL, is_ready, list_applications, report, wait_for_apps
from aif.hub_api import DevNetworkApi, HubApi
from aif.kube import ClientKubeApi, KubeApi, KubeError
from aif.teardown import AwsClients, Options, Teardown
from aif.waiting import Clock

if TYPE_CHECKING:
    from mypy_boto3_eks import EKSClient
    from mypy_boto3_ssm import SSMClient
    from mypy_boto3_sts import STSClient

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
CLUSTERS = ("hub", "dev", "prod")
ACCOUNT_ENV = "AIF_CONFIRM_ACCOUNT"
"""`aif up` also reads the account id from this variable, for the IDE first boot."""
STAGE_DELETE_TIMEOUT_SECONDS = 2700
"""Deleting stage 00 removes PlatformCluster, so ACK deletes Dev and Prod. That takes long."""


@dataclass
class Deps:
    """Everything that talks to the outside world. Tests replace these."""

    kube: Callable[[str], KubeApi] = ClientKubeApi
    sts: Callable[[str], "STSClient"] = aws.sts_client
    ssm: Callable[[str], "SSMClient"] = aws.ssm_client
    aws_clients: Callable[[str], AwsClients] = aws.clients
    hub: Callable[[str, Mapping[str, str]], HubApi] = hub_api.load_hub
    dev_network: Callable[[str, Mapping[str, str]], DevNetworkApi] = hub_api.load_dev_network
    eks: Callable[[str], "EKSClient"] = aws.eks_client
    home: Path = field(default_factory=Path.home)
    clock: Clock = field(default_factory=Clock)
    cwd: Path = field(default_factory=Path.cwd)
    env: Mapping[str, str] | None = None


@dataclass
class Ctx:
    settings: config.Settings
    stages: dict[str, stages.Stage]
    root: Path
    deps: Deps

    def env(self) -> Mapping[str, str]:
        return os.environ if self.deps.env is None else self.deps.env

    def hub(self) -> KubeApi:
        return self.deps.kube(self.settings.contexts.hub)

    def clusters(self) -> dict[str, KubeApi]:
        contexts = self.settings.contexts.as_dict()
        return {name: self.deps.kube(contexts[name]) for name in CLUSTERS}

    def kubeconfig_path(self) -> Path:
        return eks.kubeconfig_path(self.env(), self.deps.home)


def _stage_id(value: str) -> str:
    if len(value) != 2 or not value.isdigit() or int(value) > 13:
        raise argparse.ArgumentTypeError(f"stage must be 00 to 13, got {value!r}")
    return value


def _add_change_flags(cmd: argparse.ArgumentParser, env_allowed: bool) -> None:
    where = f" (or set {ACCOUNT_ENV})" if env_allowed else " (required)"
    cmd.add_argument(
        "--confirm-account",
        default="",
        metavar="ACCOUNT_ID",
        help=f"12-digit id that must match the current AWS credentials{where}",
    )
    cmd.add_argument(
        "--apply", action="store_true", help="make the changes (default: print the plan only)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aif", description=__doc__)
    parser.add_argument("--version", action="version", version=f"aif {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="build the lab")
    _add_change_flags(up, env_allowed=True)
    up.add_argument("--groups", help="comma-separated groups (default: from settings)")

    down = sub.add_parser("down", help="remove the lab")
    _add_change_flags(down, env_allowed=False)
    down.add_argument("--include-untagged", action="store_true", help="also delete untagged")
    down.add_argument(
        "--force-finalizers", action="store_true", help="remove stuck finalizers (logged)"
    )

    sub.add_parser("status", help="show the state of each stage")
    sub.add_parser("doctor", help="check mode, sign-in, health and identity")
    sub.add_parser("cost", help="sum the cost sheet per stage")

    stage = sub.add_parser("stage", help="list stages, or turn one on or off")
    stage_sub = stage.add_subparsers(dest="stage_command", required=True)
    for state in ("on", "off"):
        cmd = stage_sub.add_parser(state, help=f"turn a stage {state}")
        cmd.add_argument("stage_id", type=_stage_id, metavar="NN")
    stage_sub.add_parser("list", help="list stages")
    stage_sub.add_parser("graph", help="show what each stage needs")

    dev = sub.add_parser("dev", help="development account helpers")
    dev_sub = dev.add_subparsers(dest="dev_command", required=True)
    network = dev_sub.add_parser("network", help="stand-in for the launch stack")
    network.add_argument("action", choices=("up", "down"))
    _add_change_flags(network, env_allowed=False)
    network.add_argument(
        "--dev-standin",
        action="store_true",
        help="required: the owner's dev account has no launch stack, so make a stand-in network",
    )

    cleanup = sub.add_parser("cleanup", help="list leftover resources")
    cleanup.add_argument("--report", action="store_true", required=True, help="list only")
    return parser


def _account(ctx: Ctx, args: argparse.Namespace, env_allowed: bool) -> str | None:
    """The account id to confirm, or None (with a message) when it is missing or unclear."""
    flag: str = args.confirm_account
    from_env = ctx.env().get(ACCOUNT_ENV, "") if env_allowed else ""
    if flag and from_env and flag != from_env:
        print(f"refused: --confirm-account and {ACCOUNT_ENV} differ", file=sys.stderr)
        return None
    if not (flag or from_env):
        where = f"--confirm-account or {ACCOUNT_ENV}" if env_allowed else "--confirm-account"
        print(f"refused: {where} is required", file=sys.stderr)
        return None
    return flag or from_env


def _confirm(ctx: Ctx, args: argparse.Namespace, env_allowed: bool = False) -> bool:
    account = _account(ctx, args, env_allowed)
    if account is None:
        return False
    try:
        who = aws.confirm_account(ctx.deps.sts(ctx.settings.region), account)
    except aws.AccountMismatchError as err:
        print(f"refused: {err}", file=sys.stderr)
        return False
    print(f"account {who.account}, role {who.role}, region {ctx.settings.region}")
    return True


def _groups(ctx: Ctx, args: argparse.Namespace) -> tuple[str, ...]:
    if args.groups:
        return config.apply_values(ctx.settings, {"groups": args.groups}).groups
    return ctx.settings.groups


def cmd_up(ctx: Ctx, args: argparse.Namespace) -> int:
    if not _confirm(ctx, args, env_allowed=True):
        return EXIT_REFUSED
    s = ctx.settings
    inputs = launch.read_inputs(ctx.deps.ssm(s.region), s.region, s.argocd_mode)
    print(f"launch stack: {inputs.launch_stack_name}, VPC {inputs.vpc_id}")
    groups = _groups(ctx, args)
    order = stages.order(ctx.stages, groups)
    print("\n".join(up.plan_lines(s, groups, order)))
    if not args.apply:
        print("dry run: nothing changed. Add --apply to run this plan.")
        return EXIT_OK
    runner = up.Up(
        settings=s,
        hub_api=ctx.deps.hub(s.region, s.project_tags),
        eks_client=ctx.deps.eks(s.region),
        kube=ctx.deps.kube,
        kubeconfig=ctx.kubeconfig_path(),
        clock=ctx.deps.clock,
    )
    return EXIT_OK if runner.run(inputs, groups, order) else EXIT_FAILED


def _kept(ctx: Ctx) -> tuple[str, ...]:
    """Stacks to keep. Raises keep.KeepError when the launch stack is unknown."""
    return keep.keep_stacks(ctx.deps.ssm(ctx.settings.region), ctx.settings)


def cmd_down(ctx: Ctx, args: argparse.Namespace) -> int:
    if not _confirm(ctx, args):
        return EXIT_REFUSED
    s = ctx.settings
    try:
        kept = _kept(ctx)
    except keep.KeepError as err:
        print(f"refused: {err}. Nothing was changed.", file=sys.stderr)
        return EXIT_REFUSED
    clusters = ctx.clusters()
    clients = ctx.deps.aws_clients(s.region)
    inv = inventory.build(clusters, s.argocd_namespace, clients.tagging, s.project, kept)
    order = inventory.teardown_order(stages.order(ctx.stages, s.groups))
    print(f"keep: stacks {', '.join(kept)}, and anything tagged {keep.KEEP_TAG}=true")
    print("plan: remove these, top to bottom")
    print(inventory.format_plan(inv, order))
    print("then: agent roles, VPC blockers, Hub capabilities, Hub cluster, Hub platform")
    if not args.apply:
        print("dry run: nothing changed. Add --apply to run this plan.")
        return EXIT_OK
    if args.force_finalizers:
        print("WARNING: --force-finalizers can leave AWS resources behind", file=sys.stderr)
    opts = Options(
        project=s.project,
        argocd_namespace=s.argocd_namespace,
        hub_cluster=s.hub_cluster,
        keep_stacks=kept,
        include_untagged=args.include_untagged,
        force_finalizers=args.force_finalizers,
        stage_timeout=STAGE_DELETE_TIMEOUT_SECONDS,
    )
    hub_api = ctx.deps.hub(s.region, s.project_tags)
    teardown = Teardown(ctx.stages, clusters, clients, opts, clock=ctx.deps.clock, hub_api=hub_api)
    result = teardown.run(inv, order)
    for problem in result.problems:
        print(f"problem: {problem}", file=sys.stderr)
    for item in result.leftovers:
        print(f"left over: {item}", file=sys.stderr)
    return EXIT_OK if result.ok else EXIT_FAILED


def cmd_dev(ctx: Ctx, args: argparse.Namespace) -> int:
    """`aif dev network up|down`: the launch stack stand-in for a development account."""
    if not args.dev_standin:
        print("aif dev network: pass --dev-standin. At an event the launch stack provides it.")
        return EXIT_REFUSED
    if not _confirm(ctx, args):
        return EXIT_REFUSED
    s = ctx.settings
    verb = "create" if args.action == "up" else "remove"
    print(f"plan: {verb} the development network and the /aif/ SSM inputs in {s.region}")
    if not args.apply:
        print("dry run: nothing changed. Add --apply to run this plan.")
        return EXIT_OK
    network = ctx.deps.dev_network(s.region, s.project_tags)
    if args.action == "up":
        inputs = network.ensure()
        print(f"published inputs for VPC {inputs.vpc_id}, launch stack {inputs.launch_stack_name}")
    else:
        network.delete()
        print("development network removed")
    return EXIT_OK


def cmd_status(ctx: Ctx, _args: argparse.Namespace) -> int:
    hub, s = ctx.hub(), ctx.settings
    current = labels.read_labels(hub, s.argocd_namespace, s.hub_cluster)
    state = labels.stage_state(ctx.stages, current)
    apps = list_applications(hub, s.argocd_namespace)
    print(f"{'ID':<4}{'GROUP':<10}{'STATE':<6}{'APPS':<6}{'NOT READY'}")
    for st in ctx.stages.values():
        mine = [a for a in apps if a["metadata"].get("labels", {}).get(STAGE_LABEL) == st.id]
        bad = [report(a).app for a in mine if not is_ready(a)]
        on = "on" if state[st.id] else "off"
        print(f"{st.id:<4}{st.group:<10}{on:<6}{len(mine):<6}{', '.join(bad) or '-'}")
    return EXIT_OK


def cmd_doctor(ctx: Ctx, _args: argparse.Namespace) -> int:
    try:
        hub: KubeApi | None = ctx.hub()
    except (KubeError, OSError, ValueError) as err:
        print(f"Hub cluster: cannot load context {ctx.settings.contexts.hub}: {err}")
        hub = None
    rep = doctor.collect(
        ctx.settings, ctx.stages, hub, lambda: aws.identity(ctx.deps.sts(ctx.settings.region))
    )
    print(doctor.format_report(rep))
    return EXIT_OK if not rep.errors and not rep.unhealthy else EXIT_FAILED


def cmd_stage(ctx: Ctx, args: argparse.Namespace) -> int:
    if args.stage_command == "list":
        print(stages.format_list(ctx.stages.values()))
        return EXIT_OK
    if args.stage_command == "graph":
        print(stages.format_graph(ctx.stages))
        return EXIT_OK
    hub, s = ctx.hub(), ctx.settings
    if args.stage_command == "on":
        wanted = labels.on_labels(ctx.stages, args.stage_id)
    else:
        current = labels.read_labels(hub, s.argocd_namespace, s.hub_cluster)
        wanted = labels.off_labels(ctx.stages, args.stage_id, current)
    changed = labels.set_labels(hub, s.argocd_namespace, s.hub_cluster, wanted)
    print(f"changed: {', '.join(sorted(changed)) or 'nothing, already set'}")
    if args.stage_command == "on":
        st = ctx.stages[args.stage_id]
        wait = wait_for_apps(
            hub,
            s.argocd_namespace,
            f"{STAGE_LABEL}={st.id}",
            bringup.stage_timeout(st),
            clock=ctx.deps.clock,
        )
        print(wait.summary())
        return EXIT_OK if wait.ok else EXIT_FAILED
    return EXIT_OK


def cmd_cleanup(ctx: Ctx, _args: argparse.Namespace) -> int:
    s = ctx.settings
    try:
        kept = _kept(ctx)
    except keep.KeepError as err:
        kept = s.keep_stacks
        print(f"note: {err}. The report may list launch stack resources.")
    tagging = ctx.deps.aws_clients(s.region).tagging
    inv = inventory.build(ctx.clusters(), s.argocd_namespace, tagging, s.project, kept)
    print(inventory.format_plan(inv, inventory.teardown_order(stages.order(ctx.stages, s.groups))))
    return EXIT_OK


def cmd_cost(ctx: Ctx, _args: argparse.Namespace) -> int:
    print(cost.format_summary(cost.summarize(ctx.root / "cost" / "cost-sheet.csv")))
    return EXIT_OK


COMMANDS: dict[str, Callable[[Ctx, argparse.Namespace], int]] = {
    "up": cmd_up,
    "down": cmd_down,
    "status": cmd_status,
    "doctor": cmd_doctor,
    "stage": cmd_stage,
    "cleanup": cmd_cleanup,
    "cost": cmd_cost,
    "dev": cmd_dev,
}


def main(argv: Sequence[str] | None = None, deps: Deps | None = None) -> int:
    args = build_parser().parse_args(argv)
    deps = deps or Deps()
    try:
        root = config.find_repo_root(deps.cwd)
        settings = config.load(root, deps.env)
        ctx = Ctx(settings, stages.load_stages(root / "stages"), root, deps)
        return COMMANDS[args.command](ctx, args)
    except (config.ConfigError, stages.StageError, cost.CostSheetError) as err:
        print(f"aif: {err}", file=sys.stderr)
        return EXIT_USAGE
    except KubeError as err:
        print(f"aif {args.command}: Kubernetes API error: {err}", file=sys.stderr)
        return EXIT_FAILED
    except (BotoCoreError, ClientError) as err:
        print(f"aif {args.command}: AWS error: {err}", file=sys.stderr)
        return EXIT_FAILED
    except (eks.EksError, launch.LaunchError, up.UpError, hub_api.HubError) as err:
        print(f"aif {args.command}: {err}", file=sys.stderr)
        return EXIT_FAILED
