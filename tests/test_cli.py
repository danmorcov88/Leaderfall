"""Smoke tests for the package and CLI entry point."""

import importlib

import pytest
from typer.testing import CliRunner

from leaderfall import __version__
from leaderfall.cli import app

MODULES = ["cluster", "workload", "faults", "scenario", "measure", "report", "slo"]


@pytest.mark.parametrize("name", MODULES)
def test_modules_import(name: str) -> None:
    importlib.import_module(f"leaderfall.{name}")


@pytest.mark.parametrize("args", [["version"], ["--version"], ["-V"]])
def test_version(args: list[str]) -> None:
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0
    assert __version__ in result.output


def test_no_args_shows_help() -> None:
    result = CliRunner().invoke(app, [])
    assert "Usage" in result.output
