"""Fail unless the Python version, package.json, server.json and, if given, the git tag all agree.

Usage: python scripts/check_release.py [TAG]
"""

import json
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
version = re.search(r'__version__\s*=\s*"([^"]+)"', (root / "src/tolpo/__init__.py").read_text()).group(1)
server = json.loads((root / "server.json").read_text())

found = {"package (src/tolpo/__init__.py)": version, "server.json": server["version"]}
found["package.json"] = json.loads((root / "package.json").read_text())["version"]
for index, package in enumerate(server["packages"]):
    found[f"server.json packages[{index}]"] = package["version"]
if len(sys.argv) > 1:
    found["git tag"] = sys.argv[1].removeprefix("v")

if len(set(found.values())) != 1:
    for where, value in found.items():
        print(f"  {where}: {value}", file=sys.stderr)
    sys.exit("The versions above do not agree. Bump them together before releasing.")
print(f"All versions agree: {version}")
