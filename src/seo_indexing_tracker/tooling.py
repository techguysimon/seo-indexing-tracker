"""CLI entry points for local code quality tooling."""

from __future__ import annotations

import subprocess


def _run_tool(command: list[str]) -> None:
    completed_process = subprocess.run(command, check=False)
    if completed_process.returncode != 0:
        raise SystemExit(completed_process.returncode)


def lint() -> None:
    _run_tool(["ruff", "check", "."])


def format() -> None:
    _run_tool(["black", "."])


def typecheck() -> None:
    _run_tool(["mypy", "src", "tests"])
