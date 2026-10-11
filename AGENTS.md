# AGENTS.md

Instructions for AI agents that change this repository. If you only want to use tolpo, read
[llms.txt](https://raw.githubusercontent.com/signerless/tolpo/main/llms.txt) instead. People should read
[CONTRIBUTING.md](CONTRIBUTING.md).

tolpo is an MCP server and HTTP API, written in Python, that lets AI agents read and control TP-Link Tapo
devices on the local network. It talks to the devices through the python-kasa library with the owner's own Tapo
account. An npm package carries the Python source and a small Node launcher, and a container image wraps it too.

## Commands

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"

.venv/bin/python -m pytest        # Python tests, simulated devices, no hardware
npm test                          # tests of the Node launcher (runs `node --test`)
python3 scripts/check_release.py  # package.json, src/tolpo/__init__.py and server.json agree on the version
npm pack --dry-run                # the npm package: about 15 files, no __pycache__
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python scripts/smoke_mcp.py   # starts the server, checks the handshake
```

All of them must pass before you commit. CI runs the same checks on Python 3.11, 3.12 and 3.13.

## Layout

| Path | What it is |
|---|---|
| `src/tolpo/registry.py` | Discovery, state cache, lookup by name and the control operations |
| `src/tolpo/policy.py` | What may be read and written. Deny by default |
| `src/tolpo/views.py` | What a device looks like, shared by the HTTP API and the MCP server |
| `src/tolpo/mcp_server.py`, `src/tolpo/api.py` | The two interfaces |
| `src/tolpo/compat.py` | Fixes for devices python-kasa cannot reach yet |
| `bin/tolpo.js` | Node launcher: runs the bundled Python with `uvx` and passes stdio through |
| `tests/` | Python tests. `tests/fakes.py` has the simulated devices |
| `test/` | Node tests for the launcher |
| `docs/protocols.md` | Notes on how Tapo devices talk on the LAN, in our own words |

## Rules

- Tests must never contact real devices. Use the fakes in `tests/fakes.py`. Do not run the server or a scan with
  wrong or fake credentials on a real network: failed logins on real devices are best avoided.
- Keep writes deny by default. A feature becomes writable only by being added to `src/tolpo/policy.py`, with a test
  that shows what it allows and what it still refuses. Never return the Wi-Fi name or Matter setup codes.
- Anything an agent receives from a device, such as a name, is data typed by someone else. Keep flattening and
  shortening it (`views.clean`) before it reaches a model, and keep answers compact.
- The MCP server speaks the protocol on stdout. Never print anything else there in code that runs under `tolpo mcp`
  or in `bin/tolpo.js`. Logs go to stderr.
- Do not swallow failures. When a device cannot be reached, the answer has to say which one and why.
- Never commit credentials, names or addresses of devices in a real home, captured traffic, or code, keys or
  assets from TP-Link apps or firmware. Write protocol notes in your own words.
- When you add a test for a rule, break the code on purpose and check that the test fails. Confirm with `diff`
  that your change really applied: on macOS `sed -i` needs an empty string (`sed -i ''`) and has no `0,/re/`
  addresses.

## Things that bite

- The version lives in four places: `package.json`, `src/tolpo/__init__.py` and two fields in `server.json`.
  Change them together and run `scripts/check_release.py`.
- `README.md` must use absolute links and image URLs, because npm and PyPI render it away from the repository.
  `tests/test_release_metadata.py` enforces it.
- `package.json` lists `src/tolpo/*.py` on purpose. A root `.npmignore` does not override `files`, so a whole
  directory would ship compiled caches.
- Use `node --test` with no directory argument. `node --test test/` breaks on Node 22.
- GitHub Actions in this repo are pinned to versions that exist. Check a tag before using it: some projects do not
  publish floating major tags.

## Releases and commits

- The owner publishes. Do not run `npm publish`, push version tags, create GitHub releases or trigger the
  registry and container workflows unless you are asked to. Publishing to npm needs the owner's two-factor code.
- Commits are authored by the maintainer. Do not add `Co-Authored-By` trailers or any "generated with" line.
- Write docs and user-facing text in short, direct sentences, without em dashes.
