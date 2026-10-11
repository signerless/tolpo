"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const launcher = path.join(root, "bin", "tolpo.js");

// Put a stand-in `uvx` first on the PATH so the launcher can be tested without uv or Python.
function withFakeUvx(script, fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tolpo-uvx-"));
  fs.writeFileSync(path.join(dir, "uvx"), `#!/bin/sh\n${script}\n`, { mode: 0o755 });
  try {
    return fn(dir);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

function run(args, pathValue, input) {
  return spawnSync(process.execPath, [launcher, ...args], {
    env: { PATH: pathValue },
    input,
    encoding: "utf8",
    timeout: 10000, // a launcher that hangs must fail the test, not stall the suite
    killSignal: "SIGKILL",
  });
}

test("runs the bundled Python code through uvx and forwards the arguments", () => {
  withFakeUvx('printf "%s\\n" "$@"', (dir) => {
    const result = run(["mcp", "--config", "x.toml"], dir);
    assert.equal(result.status, 0);
    assert.deepEqual(result.stdout.trim().split("\n"), ["--from", root, "tolpo", "mcp", "--config", "x.toml"]);
  });
});

test("hands stdin and stdout to the child untouched, which the MCP protocol depends on", () => {
  withFakeUvx("/bin/cat", (dir) => {
    const message = '{"jsonrpc":"2.0","id":1,"method":"initialize"}\n';
    const result = run(["mcp"], dir, message);
    assert.equal(result.error, undefined, "the launcher hung instead of passing stdin through");
    assert.equal(result.stdout, message);
  });
});

test("passes the exit code of the child through", () => {
  withFakeUvx("exit 7", (dir) => {
    assert.equal(run(["scan"], dir).status, 7);
  });
});

test("reports a child killed by a signal the way a shell does", () => {
  withFakeUvx("kill -TERM $$", (dir) => {
    assert.equal(run(["mcp"], dir).status, 128 + 15);
  });
});

test("explains how to install uv when it is missing, and keeps stdout clean", () => {
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), "tolpo-empty-"));
  try {
    const result = run(["mcp"], empty);
    assert.equal(result.status, 127);
    assert.equal(result.stdout, "");
    assert.match(result.stderr, /uvx was not found/);
    assert.match(result.stderr, /astral\.sh\/uv/);
  } finally {
    fs.rmSync(empty, { recursive: true, force: true });
  }
});
