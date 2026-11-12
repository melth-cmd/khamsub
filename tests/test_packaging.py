"""Packaging metadata and the installed-entry-point promises."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import khamsub
import khamsub.translate

ROOT = Path(__file__).resolve().parents[1]


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_version_has_one_source():
    project = pyproject()["project"]
    assert project["dynamic"] == ["version"]
    assert khamsub.__version__ == "0.4.1"
    assert (ROOT / "khamsub" / "__init__.py").read_text().count("__version__") == 1


def test_the_console_script_points_at_the_cli():
    assert pyproject()["project"]["scripts"] == {
        "khamsub": "khamsub.translate:main"
    }


def test_the_parser_calls_itself_khamsub():
    parser = khamsub.translate.build_parser()
    assert parser.prog == "khamsub"


def test_version_prints_the_package_version(capsys):
    with pytest.raises(SystemExit) as exited:
        khamsub.translate.main(["--version"])
    assert exited.value.code == 0
    assert capsys.readouterr().out.strip() == f"khamsub {khamsub.__version__}"


def test_the_shipped_config_template_travels_in_the_package():
    template = ROOT / "khamsub" / "config.toml"
    assert template.exists()
    assert 'model = "deepseek/deepseek-v4.1-flash-1017"' in template.read_text(
        encoding="utf-8"
    )


def test_runtime_dependencies_are_declared():
    dependencies = pyproject()["project"]["dependencies"]
    names = {item.split(">")[0].split("<")[0].split("=")[0] for item in dependencies}
    assert {"srt", "charset-normalizer", "openai"} <= names


@pytest.mark.packaging
def test_a_wheel_installs_and_the_console_script_runs(tmp_path):
    """Build a wheel, install it, and run the installed console script."""
    import subprocess
    import sys
    import venv

    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    wheel = next(tmp_path.glob("khamsub-*.whl"))
    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    python = venv_dir / "bin" / "python"
    subprocess.run(
        [python, "-m", "pip", "install", str(wheel)],
        check=True,
        capture_output=True,
    )
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "PATH": "/usr/bin:/bin"}
    installed = subprocess.run(
        [venv_dir / "bin" / "khamsub", "--version"],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert installed.stdout.strip() == f"khamsub {khamsub.__version__}"

    module = subprocess.run(
        [python, "-m", "khamsub", "--version"],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert module.stdout.strip() == f"khamsub {khamsub.__version__}"

    inited = subprocess.run(
        [venv_dir / "bin" / "khamsub", "--init-config"],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert "created" in inited.stdout
    written = tmp_path / "xdg" / "khamsub" / "config.toml"
    assert "[batch]" in written.read_text(encoding="utf-8")
    assert (tmp_path / "xdg" / "khamsub" / ".env").stat().st_mode & 0o777 == 0o600
