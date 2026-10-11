#!/usr/bin/env node
"use strict";

// tolpo is a Python program. This launcher exists so that `npx tolpo mcp` works: it runs the Python code
// shipped inside this package with uvx, which builds an isolated environment the first time.
//
// The launcher must never write to stdout. With `tolpo mcp`, stdout carries the MCP protocol, and
// stdin, stdout and stderr are handed to the child process untouched.

const { spawn } = require("node:child_process");
const { constants } = require("node:os");
const path = require("node:path");

const root = path.resolve(__dirname, "..");

const MISSING_UV = `tolpo runs its Python code through uv, and uvx was not found on your PATH.

Install uv, then run this again:
  macOS and Linux:  curl -LsSf https://astral.sh/uv/install.sh | sh
  Homebrew:         brew install uv
  Windows:          powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

More options: https://docs.astral.sh/uv/getting-started/installation/
If an editor or an AI client starts tolpo, make sure it can see uv: use the full path to uvx or add its
folder to the PATH that the client uses.
`;

const child = spawn("uvx", ["--from", root, "tolpo", ...process.argv.slice(2)], { stdio: "inherit" });

child.on("error", (error) => {
  if (error.code === "ENOENT") {
    process.stderr.write(MISSING_UV);
    process.exit(127);
  }
  process.stderr.write(`tolpo could not start uvx: ${error.message}\n`);
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.exit(128 + (constants.signals[signal] || 0));
  }
  process.exit(code === null ? 1 : code);
});

for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(signal, () => child.kill(signal));
}
