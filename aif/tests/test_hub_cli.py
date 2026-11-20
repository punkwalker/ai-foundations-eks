import argparse
import unittest
from unittest import mock

from aif.config import Settings
from aif.hub import cli
from aif.hub.common import HubError, Report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aif")
    cli.register(parser.add_subparsers(dest="command", required=True))
    return parser


class ParseTest(unittest.TestCase):
    def test_hub_up_needs_the_account(self) -> None:
        with self.assertRaises(SystemExit):
            _parser().parse_args(["hub", "up"])
        args = _parser().parse_args(["hub", "up", "--confirm-account", "111122223333"])
        self.assertEqual((args.command, args.hub_command), ("hub", "up"))

    def test_dev_network_flags(self) -> None:
        args = _parser().parse_args(
            [
                "dev",
                "network",
                "up",
                "--confirm-account",
                "111122223333",
                "--admin-role-arn",
                "arn:x",
            ]
        )
        self.assertFalse(args.dev_standin)
        args = _parser().parse_args(
            ["dev", "network", "down", "--confirm-account", "111122223333", "--dev-standin"]
        )
        self.assertTrue(args.dev_standin)


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        for target in ("aif.hub.cli.aws.confirm_account", "aif.hub.cli.aws.sts_client"):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_hub_error_is_exit_1(self) -> None:
        args = _parser().parse_args(["hub", "up", "--confirm-account", "111122223333"])
        with (
            mock.patch("aif.hub.cli.hub_up.HubClients.create"),
            mock.patch("aif.hub.cli.hub_up.up", side_effect=HubError("no idc")),
        ):
            self.assertEqual(cli.COMMANDS["hub"](Settings(), args), 1)

    def test_dev_network_refused_without_flag_is_exit_1(self) -> None:
        args = _parser().parse_args(
            [
                "dev",
                "network",
                "up",
                "--confirm-account",
                "111122223333",
                "--admin-role-arn",
                "arn:x",
            ]
        )
        with mock.patch("aif.hub.cli.devnet.DevNetClients.create"):
            self.assertEqual(cli.COMMANDS["dev"](Settings(), args), 1)

    def test_dev_network_down_prints_the_report(self) -> None:
        args = _parser().parse_args(
            ["dev", "network", "down", "--confirm-account", "111122223333", "--dev-standin"]
        )
        rep = Report()
        rep.deleted("vpc")
        with (
            mock.patch("aif.hub.cli.devnet.DevNetClients.create"),
            mock.patch("aif.hub.cli.devnet.down", return_value=rep),
            mock.patch("builtins.print") as out,
        ):
            self.assertEqual(cli.COMMANDS["dev"](Settings(), args), 0)
        out.assert_called_with("deleted  vpc")
