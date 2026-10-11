# Contributing

Thanks for helping. This project controls physical devices, so every change is reviewed with safety in mind.

## Setup

```bash
git clone https://github.com/signerless/tolpo && cd tolpo
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest
```

The tests use the simulated devices in `tests/fakes.py`, so they need no hardware and no Tapo account.

## Ground rules

- Add a test for every change in behavior. A test for a safety rule should fail when you break the rule on
  purpose; check that before you open a pull request.
- Keep the policy deny-by-default. A feature becomes writable by default only by being added to
  `policy.py` deliberately, with a test that shows what it allows and what it still refuses.
- Never commit credentials, the names or addresses of devices in your own home, captured device traffic,
  or code, keys and assets extracted from TP-Link's apps or firmware. Protocol notes must be in your own words.
- Keep answers for language models compact. Every field an agent receives costs context, so leave out what
  is empty or already implied.
- Do not swallow failures. When a device cannot be reached, the answer has to say so.

## Pull requests

Describe what changed and why, and mention how you tested it. For anything that touches the policy, the
HTTP authentication or discovery, explain the failure you were guarding against.
