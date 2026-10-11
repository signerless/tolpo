"""Guards for what gets published: the registry file, the README that PyPI will render, and the version."""

import json
import re
import subprocess
import sys
from pathlib import Path

import tolpo

ROOT = Path(__file__).resolve().parent.parent
SERVER = json.loads((ROOT / "server.json").read_text())
README = (ROOT / "README.md").read_text()


def test_server_json_versions_match_the_package():
    assert SERVER["version"] == tolpo.__version__
    assert all(p["version"] == tolpo.__version__ for p in SERVER["packages"])


def test_server_json_describes_how_to_run_the_pypi_package():
    (package,) = SERVER["packages"]
    assert (package["registryType"], package["identifier"], package["runtimeHint"]) == ("pypi", "tolpo", "uvx")
    assert package["transport"] == {"type": "stdio"}
    assert package["packageArguments"] == [{"type": "positional", "value": "mcp"}]  # `uvx tolpo mcp`
    secrets = {v["name"]: v.get("isSecret", False) for v in package["environmentVariables"]}
    assert secrets == {"TAPO_EMAIL": False, "TAPO_PASSWORD": True}


def test_registry_name_uses_the_github_namespace_and_the_readme_proves_ownership():
    assert SERVER["name"] == "io.github.signerless/tolpo"
    assert f"mcp-name: {SERVER['name']}" in README
    assert len(SERVER["description"]) <= 100


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
