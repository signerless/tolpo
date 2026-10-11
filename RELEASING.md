# Releasing

tolpo is published to npm as a package that carries the Python code and runs it with uv:

```bash
npx -y tolpo mcp
```

The owner publishes to npm by hand. A workflow then lists the server in the MCP registry. PyPI is optional and
off until it is switched on.

## What a user needs

Node, to run `npx`, and [uv](https://docs.astral.sh/uv/), because the program itself is Python. The launcher
(`bin/tolpo.js`) runs `uvx` on the Python code inside the package and hands it stdin, stdout and stderr
untouched. If uv is missing it prints how to install it, on stderr.

The first run downloads about forty Python packages, so it takes longer than later runs. If an AI client
gives up waiting, run `npx -y tolpo --help` once and start the client again.

## One-time setup

Only the owner of the npm account can do this.

1. Have an npm account with two-factor authentication.
2. Log in on your machine: `npm login`.

## Every release

1. Pick the new version and change it in four places: `package.json`, `src/tolpo/__init__.py`, and the two
   `version` fields in `server.json`. Run `python scripts/check_release.py` to confirm they agree. CI runs the
   same check on every push.
2. Commit, push to `main` and wait for CI to pass.
3. Publish from a clean checkout of `main`:

   ```bash
   npm test
   npm pack --dry-run      # about 15 files, no __pycache__
   npm publish
   ```

   npm may stage the release instead of publishing it, depending on your account settings. A staged release is
   not public until you approve it with two-factor authentication. For a name that is new to npm it also leaves
   a public placeholder with the version `0.0.0-stage`, which is normal. If `npm view tolpo version` still
   shows `0.0.0-stage`, approve the staged release:

   - On the web: open the package on npmjs.com, go to the Staged Packages tab, review the release and click Approve.
   - Or in the terminal: `npm stage list tolpo`, then `npm stage approve <stage-id>`. This needs npm 11.15 or
     newer and Node 22.14 or newer.

   A published version can never be replaced, only deprecated, so check the file list first.

4. Tag the release. This runs the verification workflow, which checks that the tag matches the versions:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

5. Optional: list the server in the [MCP registry](https://modelcontextprotocol.io/registry/quickstart) as
   `io.github.signerless/tolpo`. In the Actions tab, run the workflow "Publish to the MCP registry". It waits
   until npm serves the new version, then publishes `server.json`.

## PyPI, later

The Python package is already built and checked on every push, so PyPI can be switched on whenever you want.

1. Create an account on [pypi.org](https://pypi.org) with two-factor authentication.
2. In your account settings, open Publishing and add a new pending publisher for GitHub:

   | Field | Value |
   |---|---|
   | PyPI project name | `tolpo` |
   | Owner | `signerless` |
   | Repository name | `tolpo` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

   A pending publisher does not reserve the name. The project is created by the first release, and if someone
   else registers `tolpo` before that, the pending publisher stops working.
3. In the repository settings, add the variable `PUBLISH_PYPI` with the value `true`.

From then on, pushing a version tag also publishes to PyPI, after an owner approves the deployment in the `pypi`
environment. Then `uvx tolpo mcp` works without Node. Add the PyPI package to `server.json` once it exists,
because the registry checks that every package listed there exists.
