"""Guards for what gets published: the registry file, the README that PyPI will render, and the version."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import tolpo

ROOT = Path(__file__).resolve().parent.parent
SERVER = json.loads((ROOT / "server.json").read_text())
PACKAGE = json.loads((ROOT / "package.json").read_text())
README = (ROOT / "README.md").read_text()


def test_every_version_in_the_repo_agrees():
    versions = {tolpo.__version__, SERVER["version"], PACKAGE["version"], *(p["version"] for p in SERVER["packages"])}
    assert versions == {tolpo.__version__}


def test_server_json_describes_how_to_run_the_npm_package():
    (package,) = SERVER["packages"]
    assert (package["registryType"], package["identifier"], package["runtimeHint"]) == ("npm", PACKAGE["name"], "npx")
    assert package["transport"] == {"type": "stdio"}
    assert package["packageArguments"] == [{"type": "positional", "value": "mcp"}]  # `npx tolpo mcp`
    secrets = {v["name"]: v.get("isSecret", False) for v in package["environmentVariables"]}
    assert secrets == {"TAPO_EMAIL": False, "TAPO_PASSWORD": True}


def test_registry_name_is_the_same_everywhere_it_has_to_be():
    assert SERVER["name"] == PACKAGE["mcpName"] == "io.github.signerless/tolpo"  # npm ownership check
    assert f"mcp-name: {SERVER['name']}" in README  # PyPI ownership check, for when PyPI is used
    assert len(SERVER["description"]) <= 100


def test_npm_package_ships_what_its_launcher_needs():
    launcher = ROOT / PACKAGE["bin"]["tolpo"]
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    files = PACKAGE["files"]
    assert {"bin/", "pyproject.toml", "README.md", "LICENSE"} <= set(files)
    assert "src/tolpo/*.py" in files  # explicit, so compiled caches never ship
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'readme = "README.md"' in pyproject and 'license-files = ["LICENSE"]' in pyproject  # the launcher builds from this


def test_readme_has_no_relative_paths_that_would_break_on_pypi():
    assert not re.findall(r'(?:src|srcset)="(?!https?://)[^"]+"', README)
    relative = [t for t in re.findall(r"\]\(([^)\s]+)\)", README) if not re.match(r"(https?:|#|mailto:)", t)]
    assert not relative, relative


def run_check(*args):
    return subprocess.run([sys.executable, str(ROOT / "scripts/check_release.py"), *args], capture_output=True, text=True)


def test_release_check_accepts_a_matching_tag_and_rejects_the_rest():
    assert run_check(f"v{tolpo.__version__}").returncode == 0
    assert run_check().returncode == 0
    wrong = run_check("v9.9.9")
    assert wrong.returncode != 0 and "9.9.9" in wrong.stderr


def test_release_check_notices_each_file_that_falls_out_of_step(tmp_path):
    """Run a copy of the checker on a copy of the files, once per file whose version drifts."""
    drift = {
        "package.json": lambda d: d.update(version="9.9.9"),
        "server.json": lambda d: d.update(version="9.9.9"),
    }
    for name, change in drift.items():
        tree = tmp_path / name.replace(".", "_")
        (tree / "scripts").mkdir(parents=True)
        (tree / "src/tolpo").mkdir(parents=True)
        for relative in ("scripts/check_release.py", "src/tolpo/__init__.py", "server.json", "package.json"):
            (tree / relative).write_text((ROOT / relative).read_text())
        data = json.loads((tree / name).read_text())
        change(data)
        (tree / name).write_text(json.dumps(data))
        result = subprocess.run([sys.executable, str(tree / "scripts/check_release.py")], capture_output=True, text=True)
        assert result.returncode != 0, f"{name} drifted and the checker did not notice"
        assert name in result.stderr
