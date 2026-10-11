# Security policy

`tolpo` controls physical devices, so security reports are taken seriously.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private reporting instead: open the
[Security tab](https://github.com/signerless/tolpo/security) and choose **Report a vulnerability**.
Describe what you found, how to reproduce it and the impact you expect. Reports are handled on a best-effort basis.

## In scope

- A way to make the server perform a write the policy forbids, such as pairing, a factory reset, a camera privacy
  change or a firmware install without the owner's level and the confirmation phrase.
- A way to run an action above the owner's level, or without the confirmation phrase that levels 3 and 4 need, or to
  reuse a phrase for another device or another action.
- A way to make an automation rule do more than the owner allows at the moment it runs, to start rules again after a
  stop without the owner, or to make a device name pick the target of a rule.
- A way to make the firewall tools block the machine that runs tolpo, the router, or more than the one device named,
  or to leave a block in place after its time ran out.
- A way to write outside the clip folder, the automation state folder or the other folders tolpo is configured to use.
- A way to read data the server promises never to return: the Wi-Fi name, the Matter and HomeKit setup codes, passwords, tokens, keys and MAC addresses.
- A way to get a picture from a camera the owner has not enabled in `cameras.snapshots`, or while its privacy mode is on.
- A way to make the Tapo password, the camera account or an RTSP address appear in a response, an error or a log.
- A bypass of the bearer token or of the `Host` header check on the HTTP API.
- A path by which text supplied by a device, such as a device name, could reach an agent as an instruction.

## Out of scope

- Vulnerabilities in TP-Link devices or firmware. Report those to TP-Link.
- Vulnerabilities in [python-kasa](https://github.com/python-kasa/python-kasa). Report those upstream.
- Anything that requires the attacker to already hold the owner's Tapo account credentials.

## Supported versions

Only the latest commit on `main`.
