# Camera recordings and storage

What this area adds: a look at a camera's memory card and record plan, the list of what it recorded and when, a
picture of any past event, a few seconds of live video as a short series of pictures, and copies of recordings into
a folder you choose.

It answers questions like "is the card full?", "does the garage camera record at night?", "what happened at the
front door today?" and "show me the person the hallway camera saw at 14:05".

## Tools

- `camera_storage_status`: shows the memory card's state, size, free space, since when it holds video, and whether loop recording is on.
- `camera_storage_set_loop`: turns loop recording on or off (off stops recording when the card is full). Level 2.
- `camera_storage_format`: erases the memory card and every recording on it. Level 4, needs the confirmation phrase.
- `camera_recordings_plan`: shows when the camera records, per weekday, and whether all the time or only around detections.
- `camera_recordings_set_plan`: changes the record plan; days you do not name keep their windows. Level 2.
- `camera_recordings_days`: lists the days that have video on the card, like the calendar in the Tapo app.
- `camera_recordings_day`: lists the recorded pieces of one day, with start, end, length and kind.
- `camera_recordings_events`: lists stored detections (person, motion, vehicle...) newest first, with an id for their picture.
- `camera_thumbnail_get`: shows the picture the camera stored for a recorded piece or a detection. Same opt-in as live pictures.
- `camera_clip_frames`: takes 2 to 6 live pictures over 1 to 10 seconds, to see movement. Same opt-in as live pictures.
- `camera_clip_download`: copies up to 5 minutes of recorded video into the owner's clip folder and returns the file path, never the video.

## HTTP routes

Behind the same token as the rest of the API:

- `GET /devices/{ref}/storage`, `PUT /devices/{ref}/storage/loop` (`{"on": true}`), `POST /devices/{ref}/storage/format` (`{"confirm": "..."}`)
- `GET /devices/{ref}/recordings/plan`, `PUT /devices/{ref}/recordings/plan` (`{"windows": [...], "clear_days": [...], "enabled": true}`)
- `GET /devices/{ref}/recordings/days?start_date=&end_date=`, `GET /devices/{ref}/recordings/day/{YYYY-MM-DD}`, `GET /devices/{ref}/recordings/events?since=&until=&limit=`
- `GET /devices/{ref}/thumbnail?start_time=` or `?file_id=`: a JPEG.

## Permissions

Device methods and their levels:

| Level | Methods |
|---|---|
| 0 read | `getSdCardStatus`, `getCircularRecordingConfig`, `getRecordPlan`, `searchDateWithVideo`, `searchVideoWithUTC`, `searchDetectionList` |
| 2 setting | `setRecordPlan`, `setCircularRecordingConfig` |
| 4 lifecycle | `formatSdCard` (confirmation phrase on every call) |

Pictures and recordings do not go through device methods but through the video channel the Tapo app uses (port 8800).
They follow the rule for live pictures: only cameras whose name is listed in `cameras.snapshots`, never a camera behind a
hub, never while privacy mode covers the lens, and no new try for a while after the camera rejected the login.

Copying recordings also needs a folder: set the environment variable `TOLPO_CLIP_DIR` to the full path of an existing
folder. Without it, `camera_clip_download` refuses. Files are named `<camera>-<start>-<seconds>s.ts`, with `-2`, `-3`
and so on when the name is taken. An existing file or symlink is never written through or replaced. Only the owner can
read a copy (mode 0600). While it downloads, the copy is a hidden `.part` file in the same folder; it is removed when
the copy fails or is cancelled. A copy is limited to 300 seconds of video, 512 MB and 10 minutes of transfer.

The memory card's password field (cards locked with a password report one) is never returned. Nor are MAC addresses.

## The video channel

`tolpo/media.py` speaks the app's channel. Changes in this area:

- It sends a client id (`X-Client-UUID`), one random UUID per tolpo process, like the app sends one per install.
- It reads the camera's notifications. A locked login (`device_locked`), a stream the camera ended and why
  (`stream_finish`), a closed session (`connection_closed`), an offline card or a lack of bandwidth now give a clear
  error instead of "the camera stopped sending video". Only known reasons are named; the camera's own text is not passed on.
- It asks the camera to stop a session (`do` `stop` with the session id) before it hangs up.
- A camera that refuses port 8800 while port 28800 accepts a connection, or that answers with TLS, is reported as a
  camera that serves video only over TLS with a pre-shared key. tolpo does not support that mode: it needs a key the camera
  hands to the app.
- A rejected login on a camera with the newer password hash says that the camera may be in the local access token mode,
  where its video password comes from the Tapo cloud. The camera does not say which mode it is in, so this is a hint, not a
  detection. tolpo never retries the login with another secret.
- Picture sessions (`image` for a recording's cover picture, `download` with `media_type` 2 for an event picture) and
  recording downloads (`download` with `media_type` 0). Both announce a window of 50 parts (`X-Data-Window-Size`).
  Downloads acknowledge every 25th part number with a `stream_sequence` notification carrying `X-Data-Received`, as the
  app does. The app was not seen acknowledging cover picture sessions, so tolpo does not either.

SD card playback (`sdvod`) is not built: a copy to a file covers what an agent needs.

## What is verified and what is not

Read from the owner's C225, C465 and C610: the request and answer of `getSdCardStatus` and `getRecordPlan`, including
the record plan's text form (`["0000-2400:2"]`). The video channel's login, encryption and live preview work on those
cameras already.

Tested only with fakes (FakeCaller and the fake camera in `tests/fake_camera.py`), and marked `UNVERIFIED` in the code:

- The answer of `getCircularRecordingConfig` (`{"harddisk_manage": {"harddisk": {"loop": "on"}}}` expected) and the
  shape of `setCircularRecordingConfig`, `setRecordPlan` and `formatSdCard` on a real camera.
- The answers of `searchDateWithVideo`, `searchVideoWithUTC` and `searchDetectionList`: the list names and the item
  fields. The parser reads any list in the `playback` section and the fields the app's own classes name. Older firmware
  may answer only `searchVideoOfDay`, which needs a user id; tolpo does not send it.
- The meaning of the record plan codes: 1 continuous, 2 events, 3 the low frame rate mode, from the app's code. The
  owner's cameras show 2 and 3, but which behavior each gives was not checked.
- The event type numbers: taken from the app's cloud list (6 person, 8 vehicle, 2 motion...). That the camera uses the
  same numbers is assumed.
- The picture requests: `image` with `type: playback_display`, `start_time` and `channel` (the app also sends the
  camera's MAC address, tolpo does not), and `download` with `media_type` 2 and `file_id`. Pictures typed `image/avc` or
  `image/hevc` are decoded with ffmpeg.
- The download request (`media_type` 0, `start_time` and `end_time` as text) and how a download ends on a real camera.
- That the camera holds back data until it gets acknowledgements, and that the polite stop and the client id change
  nothing on a real camera.
- The TLS detection: no camera of the owner uses TLS-PSK, so the port 28800 behavior is from the app's code only.
