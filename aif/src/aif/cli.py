"""Command line entry point for aif.

`up` and `down` change the lab. Both refuse to run unless `--confirm-account`
matches the account of the current AWS credentials, and both only print their
plan unless `--apply` is given.
"""

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from botocore.exceptions import BotoCoreError, ClientError

from aif import __version__, aws, bringup, config, cost, doctor, inventory, labels, stages
from aif.argocd import STAGE_LABEL, is_ready, list_applications, report, wait_for_apps
from aif.kube import ClientKubeApi, KubeApi, KubeError
from aif.teardown import AwsClients, Options, Teardown
from aif.waiting import Clock

if TYPE_CHECKING:
    from mypy_boto3_sts import STSClient

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
CLUSTERS = ("hub", "dev", "prod")


@dataclass
class Deps:
    """Everything that talks to the outside world. Tests replace these."""

    kube: Callable[[str], KubeApi] = ClientKubeApi
    sts: Callable[[str], "STSClient"] = aws.sts_client
    aws_clients: Callable[[str], AwsClients] = aws.clients
    clock: Clock = field(default_factory=Clock)
    cwd: Path = field(default_factory=Path.cwd)
    env: Mapping[str, str] | None = None


@dataclass
class Ctx:
    settings: config.Settings
    stages: dict[str, stages.Stage]
    root: Path
    deps: Deps

    def hub(self) -> KubeApi:
        return self.deps.kube(self.settings.contexts.hub)

    def clusters(self) -> dict[str, KubeApi]:
        contexts = self.settings.contexts.as_dict()
        return {name: self.deps.kube(contexts[name]) for name in CLUSTERS}


def _stage_id(value: str) -> str:
    if len(value) != 2 or not value.isdigit() or int(value) > 13:
        raise argparse.ArgumentTypeError(f"stage must be 00 to 13, got {value!r}")
    return value


def _add_change_flags(cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument(
        "--confirm-account",
        required=True,
        metavar="ACCOUNT_ID",
        help="12-digit id that must match the current AWS credentials",
    )
    cmd.add_argument(
        "--apply", action="store_true", help="make the changes (default: print the plan only)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aif", description=__doc__)
    parser.add_argument("--version", action="version", version=f"aif {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="build the lab")
    _add_change_flags(up)
    up.add_argument("--groups", help="comma-separated groups (default: from settings)")

    down = sub.add_parser("down", help="remove the lab")
    _add_change_flags(down)
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

    cleanup = sub.add_parser("cleanup", help="list leftover resources")
    cleanup.add_argument("--report", action="store_true", required=True, help="list only")
    return parser


def _confirm(ctx: Ctx, account: str) -> bool:
    try:
        who = aws.confirm_account(ctx.deps.sts(ctx.settings.region), account)
    except aws.AccountMismatchError as err:
        print(f"refused: {err}", file=sys.stderr)
        return False
    print(f"account {who.account}, role {who.role}, region {ctx.settings.region}")
    return True


def cmd_up(ctx: Ctx, args: argparse.Namespace) -> int:
    if not _confirm(ctx, args.confirm_account):
        return EXIT_REFUSED
    groups = ctx.settings.groups
    if args.groups:
        groups = config.apply_values(ctx.settings, {"groups": args.groups}).groups
    order = stages.order(ctx.stages, groups)
    print(f"plan: bring up {len(order)} stages, groups {', '.join(groups)}")
    print(bringup.format_plan(order))
    if not args.apply:
        print("dry run: nothing changed. Add --apply to run this plan.")
        return EXIT_OK
    s = ctx.settings
    result = bringup.bring_up(
        order, ctx.hub(), s.argocd_namespace, s.hub_cluster, clock=ctx.deps.clock
    )
    return EXIT_OK if result.ok else EXIT_FAILED


def cmd_down(ctx: Ctx, args: argparse.Namespace) -> int:
    if not _confirm(ctx, args.confirm_account):
        return EXIT_REFUSED
    s = ctx.settings
    clusters = ctx.clusters()
    clients = ctx.deps.aws_clients(s.region)
    inv = inventory.build(clusters, s.argocd_namespace, clients.tagging, s.project, s.keep_stacks)
    order = inventory.teardown_order(stages.order(ctx.stages, s.groups))
    print("plan: remove these, top to bottom")
    print(inventory.format_plan(inv, order))
    if not args.apply:
        print("dry run: nothing changed. Add --apply to run this plan.")
        return EXIT_OK
    if args.force_finalizers:
        print("WARNING: --force-finalizers can leave AWS resources behind", file=sys.stderr)
    opts = Options(
        project=s.project,
        argocd_namespace=s.argocd_namespace,
        hub_cluster=s.hub_cluster,
        keep_stacks=s.keep_stacks,
        include_untagged=args.include_untagged,
        force_finalizers=args.force_finalizers,
    )
    result = Teardown(ctx.stages, clusters, clients, opts, clock=ctx.deps.clock).run(inv, order)
    for problem in result.problems:
        print(f"problem: {problem}", file=sys.stderr)
    for item in result.leftovers:
        print(f"left over: {item}", file=sys.stderr)
    return EXIT_OK if result.ok else EXIT_FAILED


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
    tagging = ctx.deps.aws_clients(s.region).tagging
    inv = inventory.build(ctx.clusters(), s.argocd_namespace, tagging, s.project, s.keep_stacks)
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
