"""Tests for the aif command line stubs.

These tests use unittest assertion methods instead of bare `assert`, so that
bandit (rule B101) and ASH report no findings on them.
"""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from aif import __version__
from aif.cli import NOT_IMPLEMENTED_EXIT, main

STUB_COMMANDS = (
    ["up"],
    ["down"],
    ["status"],
    ["doctor"],
    ["cost"],
    ["stage", "on", "03"],
    ["stage", "off", "13"],
    ["cleanup", "--report"],
)

ARGPARSE_USAGE_EXIT = 2


class VersionTest(unittest.TestCase):
    def test_version_prints_package_version(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as exc:
            main(["--version"])
        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"aif {__version__}")


class StubCommandTest(unittest.TestCase):
    def test_stub_commands_exit_not_implemented(self) -> None:
        for argv in STUB_COMMANDS:
            with self.subTest(argv=argv):
                err = io.StringIO()
                with redirect_stderr(err):
                    code = main(argv)
                self.assertEqual(code, NOT_IMPLEMENTED_EXIT)
                self.assertIn("not implemented", err.getvalue())

    def test_stage_rejects_bad_id(self) -> None:
        for bad in ("3", "14", "ab"):
            with self.subTest(stage=bad):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                    main(["stage", "on", bad])
                self.assertEqual(exc.exception.code, ARGPARSE_USAGE_EXIT)
