"""Tests for the aif command line stubs."""

import pytest

from aif import __version__
from aif.cli import NOT_IMPLEMENTED_EXIT, main


def test_version_prints_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"aif {__version__}"


@pytest.mark.parametrize(
    "argv",
    [
        ["up"],
        ["down"],
        ["status"],
        ["doctor"],
        ["cost"],
        ["stage", "on", "03"],
        ["cleanup", "--report"],
    ],
)
def test_stub_commands_exit_not_implemented(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == NOT_IMPLEMENTED_EXIT
    assert "not implemented" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["3", "14", "ab"])
def test_stage_rejects_bad_id(bad: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["stage", "on", bad])
    assert exc.value.code == 2
