# Security policy

`agentic-home` controls physical devices, so security reports are taken seriously.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private reporting instead: open the
[Security tab](https://github.com/signerless/agentic-home/security) and choose **Report a vulnerability**.
Describe what you found, how to reproduce it and the impact you expect. Reports are handled on a best-effort basis.

## In scope

- A way to make the server perform a write the policy forbids, such as `pair`, `unpair`, camera privacy changes
  or a reboot without an explicit opt-in.
- A way to read data the server promises never to return: the Wi-Fi name and Matter setup codes.
- A bypass of the bearer token or of the `Host` header check on the HTTP API.
- A path by which text supplied by a device, such as a device name, could reach an agent as an instruction.

## Out of scope

- Vulnerabilities in TP-Link devices or firmware. Report those to TP-Link.
- Vulnerabilities in [python-kasa](https://github.com/python-kasa/python-kasa). Report those upstream.
- Anything that requires the attacker to already hold the owner's Tapo account credentials.

## Supported versions

Only the latest commit on `main`.
