# How Tapo devices talk on the local network

These notes describe how the official Tapo app and Tapo devices communicate over the LAN, as far as we
could establish it. They exist so that other people can write interoperable software for devices they own.

## Where this comes from

- **Independent analysis of the official app.** We downloaded the Android build 3.21.114 (package
  `com.tplink.iot`) from a public mirror, checked its hash and signing certificate against the published
  values, and read its structure with a standard decompiler (jadx) in order to understand how it talks to devices.
- **Observation of real devices.** Everything in the tables below that says "observed" was seen on devices
  we own, by reading their discovery responses and by watching which transport an open source client
  negotiates with them.
- **Existing open source work.** The transports that `python-kasa` implements are documented in its
  repository. We link to it rather than repeat it. The video channel of the cameras is described by
  [go2rtc](https://github.com/AlexxIT/go2rtc) and [pytapo](https://github.com/JurajNyiri/pytapo), both MIT licensed.

What is deliberately not here: any code or assets from the app, any key, seed or default credential, and
message-level details of the newer handshake. These notes describe the shape of the protocols, not a way
into a device that is not yours. tolpo only ever authenticates with the owner's own account.

## Overview

The app can reach a device in several ways. The names are the app's own labels for them.

| Transport | What it is | Status in tolpo |
|---|---|---|
| Legacy HTTP (secure pass-through) | JSON over HTTP with an RSA-wrapped session; `python-kasa` documents it for older firmware | Handled by `python-kasa` |
| KLAP | Two-step handshake over HTTP that derives a per-session key from both sides' random seeds and a hash of the account credentials; requests are then encrypted and signed with a sequence number | Handled by `python-kasa` |
| HTTPS with AES | JSON requests over TLS on port 443, after a login step; observed on hubs and cameras | Handled by `python-kasa` |
| Media channel | Port 8800 on cameras. Digest login, then AES-encrypted MPEG-TS in multipart chunks. The app uses it for live view, see "Camera video" below | Used for pictures, see `media.py` |
| RTSP and ONVIF | The standard camera interfaces, on ports 554 and 2020, available after a camera account exists | Used for pictures when a camera account is configured |
| TPAP | A newer protocol built on a password-authenticated key exchange, see below | Not implemented. `python-kasa` 0.11 has no TPAP transport, and we know of no other open source client that does |
| BLE and TPAP over BLE | Bluetooth Low Energy, which the app uses among other things to onboard devices | Not used by tolpo |
| BCCP | A transport tied to some light strips and to music-synchronised lighting | Not analysed |

The app also talks to the vendor cloud for remote access. tolpo never does.

## Discovery

Devices answer a small UDP probe on port 20002. Both a broadcast and a probe addressed to a single device
work. The reply is a JSON document that, among other things, names the model, the device family (observed:
`SMART.TAPOBULB`, `SMART.TAPOHUB`, `SMART.IPCAMERA`) and whether HTTPS is supported. Devices on KLAP name their
scheme (`KLAP`, on port 80). The hub and the cameras instead list numeric encryption types, whose meaning we
did not establish, so tolpo does not interpret them: it hands the reply to `python-kasa`, which picks the
transport.

Two practical lessons from real networks:

- Broadcast is lossy. Battery-powered cameras answer it only some of the time, yet answer a probe addressed
  directly to them. Asking each address in the subnet one by one finds them.
- A device can answer discovery and still refuse to connect. Always report it instead of hiding it.

## TPAP

TPAP is the app's newer local protocol. Its core is SPAKE2+, a password-authenticated key exchange standardised
as [RFC 9383](https://www.rfc-editor.org/rfc/rfc9383). The password never travels; both sides derive a shared
secret from it, confirm that they derived the same one, and then protect traffic with a cipher that the device
names, keyed from that secret, with a sequence number on each message.

What we established about its shape:

- The client first asks the device what it supports, then registers, then exchanges key shares and
  confirmations. The device may also present a certificate chain and a signature to prove that it is genuine,
  which a client can verify.
- The device advertises how the credential is to be prepared before it enters the exchange. The schemes seen
  are ordinary, public ones, such as salted SHA-256 and MD5 crypt hashing.
- Nine cipher suites are defined, combining SHA-256 or SHA-512, HKDF, and either HMAC or AES-CMAC.

What we did not establish, and why it matters: none of the devices we own use TPAP today, so there was
nothing to test against. A client written from these notes alone would be guesswork. If you own a device that
negotiates TPAP and want to help, say so in an issue.

## Camera video

Cameras give video in two ways.

**RTSP.** After you create a camera account in the Tapo app, a camera serves its main stream at `/stream1` and a
smaller one at `/stream2` on port 554, and ONVIF on port 2020. `python-kasa` builds the same address. The cameras we
own listened on neither port before we made an account, so this route needs that extra setup.

**The channel of the Tapo app.** All three cameras we own also listen on port 8800. A plain request there gets an
HTTP Digest challenge, which is how the app starts live view. It needs only the Tapo account. As go2rtc and pytapo
describe it, and as we checked on one of our own cameras:

1. `POST /stream` gets a 401 with a Digest challenge. We saw MD5, `qop=auth` and an encrypt type of 3.
2. The client answers as the user `admin`. The secret in the Digest is the Tapo account password as upper-case hex,
   hashed with SHA-256 when the challenge says encrypt type 3 and with MD5 otherwise.
3. The camera answers 200 with a `Key-Exchange` header holding a nonce. The media is AES encrypted with a key and an
   IV derived from that nonce and the same hashed password.
4. The client sends one JSON message asking for a live preview: channel, resolution, audio. The camera replies with
   multipart parts. Those typed `video/mp2t` carry MPEG-TS with H.264 video and a private second track.

The same boundary strings, headers and request fields appear in the streaming code of the app, which is how we
cross-checked the public descriptions. The second track, and padding packets at the start of some streams, make
ffmpeg's guessing stall on a live pipe. So tolpo reads the program tables itself and gives ffmpeg only the raw video.

tolpo signs in once per attempt, never retries a rejected login, and refuses a session without encryption. It uses
only the owner's own account.

The same session carries more than live view. As the app's streaming code and the public descriptions show, a
client can also:

- Send a client id header, one random value per install, as the app does.
- Read notices from the camera between the media parts. They say why a session ended: the stream finished, another
  client took the camera, the card went offline, or there was not enough bandwidth. tolpo turns the ones it knows
  into plain messages and never passes the camera's own text on.
- Ask for a recording's cover picture, an event picture, or a recording's video. Each request announces a window of
  parts the camera may send before it waits. For downloads the client acknowledges part numbers as it goes, in a
  notification that names the last part received. A camera that is not acknowledged stalls.
- End a session politely with a stop request before it hangs up.

Two variations exist that tolpo detects and names but does not support. Some cameras reject the account password on
port 8800 because they are in a mode where the app uses a token handed out by the cloud. Some answer on port 28800
instead, with TLS and a pre-shared key. Both need secrets that do not come from the owner's own account.

Two-way audio and live playback from the memory card use related requests. They are not implemented. Every shape in
this paragraph was checked against fakes and the public descriptions, and the area page marks what no real camera of
ours has confirmed.

## What the devices we tested use

All observed through `python-kasa` against real devices on one home network.

| Model | Kind | Transport observed |
|---|---|---|
| L430C | Colour bulb | KLAP |
| P115 | Smart plug with energy readings | KLAP |
| H200 | Hub | HTTPS with AES |
| T110, T300, T310, D230 | Sensors and doorbell paired to the hub | Through the hub, no own address |
| C225, C610, C465 | Cameras | HTTPS with AES |

Notes:

- The C465 presents a certificate that uses ECDSA, while `python-kasa` offers only RSA cipher suites. The two
  share no suite and the handshake ends with a misleading `ASN1` error. Offering two ECDSA suites as well fixes
  it, and tolpo does so in `compat.py`.
- A hub takes a while to load its children the first time, up to about ten seconds, and a few seconds after
  that.
- Battery cameras are the slowest and least predictable devices to connect to.

## Device families the app knows

From the structure of the app, not from testing: lights, plugs, hubs with sensors, cameras and doorbells,
robot vacuums, door locks, and Matter devices. tolpo has been tried on lights, a plug, a hub with sensors,
cameras and a doorbell.

## Corrections welcome

If something here is wrong or out of date, open an issue. Please keep contributions in your own words and
free of vendor code, keys or captured credentials, as described in [CONTRIBUTING.md](../CONTRIBUTING.md).
