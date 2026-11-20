"""Command line for `aif hub up` and `aif dev network up|down`.

W2 owns `aif/cli.py`. To wire these in, it calls `register(sub)` from `build_parser`
and adds `COMMANDS` from this module to its dispatch table. Each handler takes the
loaded Settings and the parsed arguments and returns an exit code.
"""

import argparse
from collections.abc import Callable

from aif import aws, devnet
from aif.config import Settings
from aif.hub import up as hub_up
from aif.hub.common import HubError, Report
from aif.hub.inputs import InputError


def _change_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--confirm-account", required=True, help="12-digit account the credentials must be for"
    )


def register(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    hub = sub.add_parser("hub", help="bring up the Hub cluster and its capabilities")
    hub_sub = hub.add_subparsers(dest="hub_command", required=True)
    _change_flags(hub_sub.add_parser("up", help="create or update the Hub (safe to rerun)"))

    dev = sub.add_parser("dev", help="dev-account helpers, refused at events")
    dev_sub = dev.add_subparsers(dest="dev_command", required=True)
    net = dev_sub.add_parser("network", help="stand-in for the launch template's network")
    net_sub = net.add_subparsers(dest="network_command", required=True)
    for name in ("up", "down"):
        cmd = net_sub.add_parser(name, help=f"{name} the stand-in VPC and SSM inputs")
        _change_flags(cmd)
        cmd.add_argument("--dev-standin", action="store_true", help="required: confirm dev account")
        if name == "up":
            cmd.add_argument("--admin-role-arn", required=True, help="role that gets cluster admin")


def _print(report: Report) -> None:
    for line in report.lines:
        print(line)


def cmd_hub(settings: Settings, args: argparse.Namespace) -> int:
    aws.confirm_account(aws.sts_client(settings.region), args.confirm_account)
    clients = hub_up.HubClients.create(settings.region)
    try:
        result = hub_up.up(
            clients,
            settings.region,
            settings.project_tags,
            hub_up.HubOptions.from_env(argocd_mode=settings.argocd_mode),
        )
    except (HubError, InputError) as err:
        print(f"aif hub up: {err}")
        return 1
    _print(result.report)
    return 0


def cmd_dev(settings: Settings, args: argparse.Namespace) -> int:
    aws.confirm_account(aws.sts_client(settings.region), args.confirm_account)
    clients = devnet.DevNetClients.create(settings.region)
    try:
        if args.network_command == "up":
            report = devnet.up(
                clients,
                settings.region,
                settings.project_tags,
                args.admin_role_arn,
                dev_standin=args.dev_standin,
            )
        else:
            report = devnet.down(clients, settings.project_tags, dev_standin=args.dev_standin)
    except (HubError, devnet.StandInRefusedError) as err:
        print(f"aif dev network {args.network_command}: {err}")
        return 1
    _print(report)
    return 0


COMMANDS: dict[str, Callable[[Settings, argparse.Namespace], int]] = {
    "hub": cmd_hub,
    "dev": cmd_dev,
}
