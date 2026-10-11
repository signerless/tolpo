# Releasing

tolpo is published from GitHub Actions in a single run, started by pushing a version tag. No API token is
stored anywhere: PyPI trusts this repository and the MCP registry trusts its GitHub identity.

The run has three stages, each waiting for the one before it:

1. Build. It checks that the tag matches the version in the code, runs the tests, builds the package and
   checks that its page will render on PyPI.
2. Publish to PyPI, after an owner approves it in the `pypi` environment.
3. Publish to the [MCP registry](https://modelcontextprotocol.io/registry/quickstart) as
   `io.github.signerless/tolpo`, once PyPI is serving the new version.

## One-time setup

Only the owner of the PyPI account can do this.

1. Create an account on [pypi.org](https://pypi.org) and turn on two-factor authentication.
2. In your PyPI account settings, open Publishing and add a new pending publisher for GitHub with these values:

   | Field | Value |
   |---|---|
   | PyPI project name | `tolpo` |
   | Owner | `signerless` |
   | Repository name | `tolpo` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

A pending publisher does not reserve the name. The project is created by the first release, and if someone
else registers `tolpo` before that, the pending publisher stops working. Publish the first release soon
after you set it up. After it, the pending publisher becomes a normal trusted publisher.

The `pypi` environment already exists in this repository and requires an owner to approve each deployment, so
a tag pushed by mistake cannot publish by itself.

## Every release

1. Pick the new version number and change it in three places: `src/tolpo/__init__.py`, and the two `version`
   fields in `server.json`. Run `python scripts/check_release.py` to confirm they agree. CI runs the same
   check on every push.
2. Commit, push to `main` and wait for CI to pass.
3. Tag and push the tag:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

4. Open the run in the Actions tab and approve the `pypi` deployment when GitHub asks.

A version on PyPI can never be replaced, only yanked. If something is wrong, fix it and release the next
version.

## Using a release

```bash
uvx tolpo mcp                      # run once, without installing
claude mcp add tolpo -e TAPO_EMAIL=you@example.com -e TAPO_PASSWORD=... -- uvx tolpo mcp
```

The first run downloads about forty packages, so it takes longer than later runs.
