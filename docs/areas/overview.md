# Overview

- `tolpo_areas`: lists the parts of tolpo (areas such as lights, plugs and cameras), whether each is on and why, and the tools each one offers. An agent can call it first, instead of guessing tool names.

Which areas are on:

- By default, the areas for kinds of devices you do not have are off. tolpo knows your devices from earlier runs (the host cache), so a home
  without a robot vacuum or a door lock does not load their tools. Areas that are not tied to one kind of device (automation, network,
  pairing, device admin) are on unless you choose the areas yourself. If tolpo has not seen your devices yet, or meets a model it does not
  recognize, nothing is hidden. The doorbell area is on when there is a hub, a doorbell or a camera.
- To choose yourself, list the areas under `areas` in the `[server]` section of the config file, or set `TOLPO_AREAS` (names separated by
  commas). `all` turns everything on. Your list wins over the default.
- An area that is off adds no tools and no HTTP routes, which keeps the tool list short for AI clients that load every tool description.
