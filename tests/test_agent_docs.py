"""AGENTS.md and llms.txt are read by other agents, so they must not drift from the code they describe."""

import re
from pathlib import Path

import pytest
from mcp import Client

from tolpo.mcp_server import build_server

from .test_mcp import make

ROOT = Path(__file__).resolve().parent.parent
LLMS = (ROOT / "llms.txt").read_text()
AGENTS = (ROOT / "AGENTS.md").read_text()
SOURCES = ("https://raw.githubusercontent.com/signerless/tolpo/main/", "https://github.com/signerless/tolpo/blob/main/")


def test_llms_txt_follows_the_format():
    lines = LLMS.splitlines()
    assert lines[0] == "# tolpo"
    assert lines[2].startswith("> ")  # the summary, in a blockquote
    h2 = [line for line in lines if line.startswith("## ")]
    assert h2[-1] == "## Optional"  # secondary links last, by convention
    links = re.findall(r"\]\(([^)\s]+)\)", LLMS)
    assert links and all(url.startswith("https://") for url in links)  # an agent reads it away from the repo


def test_llms_txt_links_to_files_that_exist():
    for url in re.findall(r"\]\(([^)\s]+)\)", LLMS):
        for prefix in SOURCES:
            if url.startswith(prefix):
                assert (ROOT / url.removeprefix(prefix)).is_file(), f"{url} points at a file that is not in the repo"


@pytest.mark.anyio
async def test_llms_txt_lists_every_tool_the_server_has():
    reg, _ = make()
    async with Client(build_server(reg, discover=False)) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
    listed = set(re.findall(r"^- `(\w+)`:", LLMS, flags=re.M))
    areas_doc = {
        tool
        for page in (ROOT / "docs/areas").glob("*.md")
        if page.name != "README.md"
        for tool in re.findall(r"^- `(\w+)`:", page.read_text(), flags=re.M)
    }
    assert listed <= tools, f"llms.txt documents tools the server does not have: {sorted(listed - tools)}"
    assert tools <= listed | areas_doc, f"tools nobody documented (llms.txt or docs/areas/<area>.md): {sorted(tools - listed - areas_doc)}"


def test_agents_md_names_only_files_that_exist():
    mentioned = re.findall(r"`((?:[\w.-]+/)+[\w.*-]+(?:\.\w+)?/?)`", AGENTS)
    checked = [p for p in mentioned if "*" not in p and " " not in p and not p.startswith(("~", "."))]
    assert checked, "expected AGENTS.md to mention repository paths"
    for path in checked:
        assert (ROOT / path).exists(), f"AGENTS.md mentions {path}, which is not in the repo"


def test_agents_md_commands_use_scripts_that_exist():
    for script in re.findall(r"\b(scripts/[\w.]+)", AGENTS):
        assert (ROOT / script).is_file(), script


def test_agents_md_keeps_the_maintainers_rules():
    assert "Co-Authored-By" in AGENTS  # commits are authored by the maintainer only
    assert "Do not run `npm publish`" in AGENTS  # the owner publishes
    assert "tests/fakes.py" in AGENTS  # tests never touch real devices
