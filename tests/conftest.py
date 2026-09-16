"""Shared fixtures.

The application is a single extensionless file (src/ai_log_analyzer), so it is
loaded here explicitly and registered as the ``ai_log_analyzer`` module.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "ai_log_analyzer"


def _load_module():
    loader = importlib.machinery.SourceFileLoader("ai_log_analyzer", str(SOURCE))
    spec = importlib.util.spec_from_loader("ai_log_analyzer", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    sys.modules["ai_log_analyzer"] = module
    return module


ala = _load_module()


@pytest.fixture(scope="session")
def module():
    return ala


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "ai-log-analyzer.conf"
    ala.write_default_config(path)
    return path


@pytest.fixture
def log_file(tmp_path: Path) -> Callable[[List[str], str], Path]:
    def _write(lines: List[str], name: str = "test.log") -> Path:
        path = tmp_path / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    return _write


@pytest.fixture
def run_cli(capsys, monkeypatch, config_path: Path):
    """Run main() with --config and --mock-ai; returns (exit_code, stdout, stderr)."""

    def _run(*argv: str, stdin: Optional[str] = None, mock: bool = True, warn: bool = False) -> Tuple[int, str, str]:
        if stdin is not None:
            monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        args = ["--config", str(config_path), "--no-progress", *argv]
        if mock:
            args.append("--mock-ai")
        else:
            args.append("--no-endpoint-check")
            if not warn:
                args.append("--no-warn")
        code = ala.main(args)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


def make_lines(count: int, template: str = "2026-09-16T10:{m:02d}:{s:02d}+02:00 host sshd[{i}]: Failed password for root from 10.0.0.{ip} port 2222 ssh2") -> List[str]:
    return [template.format(i=i, m=(i // 60) % 60, s=i % 60, ip=i % 250 + 1) for i in range(1, count + 1)]
