# Overview

- `tolpo_areas`: lists the parts of tolpo (areas such as lights, plugs and cameras), whether each is on, and the tools each one offers. An agent can call it first, instead of guessing tool names.

The owner can switch areas on and off with `areas` under `[server]` in the config file, or with the environment variable `TOLPO_AREAS`
(names separated by commas). When it is not set, every area is on. An area that is off adds no tools and no HTTP routes, which keeps the
list of tools short for AI clients that load every tool description.
