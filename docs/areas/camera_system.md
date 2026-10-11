# Camera PTZ and settings

These tools change how a camera or doorbell moves, looks, sounds and runs. They use the owner's existing camera
session. Most calls read the camera's component list first and refuse a feature the model does not report. The
PTZ stop is the exception: it is always sent. Writes refresh the device state afterwards.

The tests use fakes and sanitized read responses from the owner's C225, C465, C610 and the hub-managed D230 doorbell.
The end of this page says what was also tried on real devices: reads, and one write (the microphone volume).

## Pan and tilt

Movement is level 1. Viewpoints, patrol and automatic tracking are level 2.

- `camera_ptz_status`: Read viewpoints, angle ranges, patrol points, patrol status or tracking.
- `camera_ptz_stop`: Stop pan and tilt movement. Sent without a capability read, so it works when other reads fail.
- `camera_ptz_cruise`: Sweep once horizontally or vertically, or stop the sweep.
- `camera_ptz_calibrate`: Drive the motor to its end stops, then resume at the factory or saved position.
- `camera_ptz_save_preset`: Save the current viewpoint with a plain name.
- `camera_ptz_delete_presets`: Delete selected existing viewpoints.
- `camera_ptz_patrol`: Start or stop the configured patrol.
- `camera_ptz_patrol_point`: Add, edit or delete one patrol point. Add and edit need a name and a wait time.
- `camera_ptz_auto_tracking`: Enable or disable basic target tracking.

There is no speed or step size to set here. A sweep covers the range once. Step moves stay with python-kasa's
pan and tilt features. A sweep stops with `camera_ptz_cruise` and a patrol with `camera_ptz_patrol`.
Absolute movement is left out: the audit names `absoluteMove` but not its module and section.
Patrol schedules, ordering, attributes and per-target tracking are left out until their payloads are known.
Check on the C225 and C610: stop during a sweep and a patrol, patrol point ids, and the response wrappers.

## Image, indicator light and privacy

Image modes, HDR and overlays are level 2. Night vision, flip, style and HDR accept only values the camera reports.
Overlays keep their position and time format. Multi-lens image writes and battery camera overlays are refused.

- `camera_image_read`: Read image settings, privacy regions, overlays and supported ranges.
- `camera_image_mode`: Set night vision, flip or mirror, image style, or main-stream HDR (on or off).
- `camera_image_osd`: Show or hide the time, name or logo overlay, and change the plain name text.
- `camera_image_privacy_zones`: Enable or disable existing blacked-out regions, with level 3 confirmation.
- `camera_image_led`: Turn a version 1 indicator light on or off, with level 3 confirmation.
- `camera_system_privacy`: Close or open the lens mask, with level 3 confirmation.

The indicator light is level 3 in both directions: a dark LED hides that the camera records. `setLedStatus` must
have the same level in every area that declares it. Privacy zones are level 3 too: off shows areas the owner
blacked out. Privacy mode OFF is level 3 with the phrase `setLensMaskConfig <device id>`. Privacy mode ON also needs
level 3 for now, because a declared method has one level whatever its parameters. Closing the mask at level 1 needs
a parameter-aware rule in the shared `policy.py` and `calls.py`. The audit found no privacy schedule, so none exists.
Region editing, LED color and version 2 LED writes are left out until their payloads are known.

UNVERIFIED: the audit could not resolve the module and section of `setHDR`. tolpo sends `video.set_hdr` with
`secname` and `hdr`. Check on a camera: image ranges, HDR values, the overlay table layout and the lens mask write.

## Battery cameras

- `camera_battery_read`: Read status, charging, modes, capability ranges, PIR sensitivity or wake-up settings.
- `camera_battery_mode`: Set a reported power source, a working mode, or the wake-up trigger, at level 2.
- `camera_battery_clips`: Set clip length, buffer and retrigger time at level 2.
- `camera_battery_charging_privacy`: Change privacy while charging, with level 3 confirmation.
- `camera_battery_statistics`: Read up to 31 days of compact battery and solar usage.

Battery module requests need battery component version 3 or later. Clip and wake-up reads handle the flat version 1
and the nested version 2 forms, and clip writes pick the matching form. Version 1 PIR reads use `pir_verify`.
Clip times accept 0 to 3600 seconds, with a clip length of at least 1. The camera may enforce a narrower range.
Working mode sends `follow_config=true`. Custom force parameters, charging adapter mode and PIR tuning are left out.
No tool wakes a sleeping camera. A camera that does not answer on the LAN may need BLE.

Checked on a C465 and a C610: charging privacy is read with `getChargingMode` (field `charging_privacy_mode`, next to
`adapter_mode`). Both cameras refuse `getChargingPrivacyMode`, so tolpo does not use it. UNVERIFIED: it is switched with
`setChargingMode`, as the app does. No write was tried.
Check on the C465 or C610 and the D230: capability ranges, working mode and the charging privacy write.

## Audio

- `camera_audio_read`: Read microphone, speaker, audio recording and noise processing states.
- `camera_audio_set`: Set microphone or speaker volume, recorded audio, or voice enhancement, at level 2.

Volume settings take `volume` (0 to 100) and switches take `enabled`. Microphone writes keep the current codec,
sample rate, channels and bitrate. Noise suppression and echo cancellation are read only: no write method is known.
Sound playback and talk-back need the media channel and level 3. They are not here.
Check both volume ends on a camera and the doorbell.

## System

- `camera_system_read`: Read privacy mode, time, Wi-Fi signal, UPnP, compatibility, account status, reboot or firmware state.
- `camera_system_timezone`: Set an IANA time zone and NTP mode at level 2.
- `camera_system_upnp`: Enable or disable UPnP port mapping with level 3 confirmation. Keeps the mapping mode.
- `camera_system_compatibility`: Change the third-party compatibility flag with level 4 confirmation.
- `camera_system_account_enabled`: Turn an existing RTSP and ONVIF login on or off with level 3 confirmation.
- `camera_system_reboot`: Restart the camera at level 2.
- `camera_system_reboot_schedule`: Turn scheduled restarts on or off or change their time at level 2. Keeps days and spread.
- `camera_system_firmware_check`: Ask the camera to check the vendor firmware catalogue at level 1. Installs nothing.
- `camera_system_firmware_update`: Start a firmware install with level 4 confirmation.
- `camera_system_auto_update`: Change unattended updates with level 4 confirmation. Keeps the update window.
- `camera_system_reset`: Factory reset or restore defaults with level 4 confirmation.

UPnP on lets the camera ask the router to open a port, which can make it reachable from the internet.
Third-party compatibility off may stop local clients, tolpo included, from reaching the camera. Only the Tapo app
can turn it back on, so the tool is level 4 and says so. Only the device flag changes. The cloud account flag and
the certificate calls are left out.
Camera accounts cannot be created here: their password encryption needs key material. An existing account can be
read for its enabled state and switched. Its username and password never leave the tool and are never logged.
Wi-Fi reads leave out the SSID, addresses and MAC. Wi-Fi responses are flat, as in all three camera fixtures.
Time sync selects NTP mode. The camera sets its own clock.
Factory reset sends `softReset` with `type=factory` on system version 4 or battery cameras, and `reset` otherwise.
The confirmation phrase names the method that will be sent. Restore sends `configRestore`.
The camera downloads firmware from the vendor itself. tolpo supplies no image or key.
Reboot, reset and firmware writes try a state refresh. An outage during that refresh is reported as an error
that says the command was accepted.
Hub-managed cameras read their components through `getChildDeviceComponentList` on the parent hub.

Checked on a real D230: `getBatteryStatistic` answers `{"statistic": {"day_num", "day": [...]}}` with no `battery` around
it, and tolpo reads that shape (it still accepts the wrapped one).
UNVERIFIED: `getPatrolInfo` is read under `patrol.get_patrol_info`. A C225, a C465 and a C610 refuse
`getFirmwareCapability` with PROTOCOL_FORMAT_ERROR for every request shape tried, so changing automatic updates does not
wait for it: it reads the current update configuration instead, and a camera that does answer the capability read with "no"
is still refused. Check on hardware: reboot, reset and firmware commands and the connection afterwards, and UPnP mapping modes.

Unexpected or rejected responses raise an error naming the device and method. Raw device error text is hidden.

## Declared device methods

- Level 0: `getAudioConfig`, `getBatteryCapability`, `getBatteryOperatingMode`, `getBatteryStatistic`, `getBatteryStatus`, `getChargingMode`, `getChildDeviceComponentList`, `getClipsConfig`, `getClockStatus`, `getCloudConfig`, `getConnectionType`, `getCoverConfig`, `getCoverRegion`, `getFirmwareAutoUpgradeConfig`, `getFirmwareCapability`, `getFirmwareUpdateStatus`, `getImageCapability`, `getImageStyleCapability`, `getImageStyleConfig`, `getLedStatus`, `getLensMaskConfig`, `getMotorAngleRange`, `getNightVisionCapability`, `getNightVisionModeConfig`, `getOsd`, `getOsdCapability`, `getPatrolInfo`, `getPatrolStatus`, `getPirSensitivity`, `getPowerMode`, `getPresetConfig`, `getReboot`, `getRotationStatus`, `getTargetTrackConfig`, `getThirdAccount`, `getThirdPartyCompatibility`, `getTimezone`, `getUpnpInfo`, `getVideoCapability`, `getVoiceEnhancementConfig`, `getWakeUpConfig`.
- Level 1: `checkFirmwareVersionByCloud`, `cruiseMove`, `cruiseStop`, `manualCalibrate`, `stopMove`.
- Level 2: `addMotorPostion`, `addPatrol`, `deletePatrol`, `deletePreset`, `editPatrol`, `rebootDevice`, `setBatteryOperatingMode`, `setClipsConfig`, `setHDR`, `setImageStyleConfig`, `setMicrophoneVolume`, `setNightVisionModeConfig`, `setOsd`, `setPatrolStatus`, `setPowerMode`, `setReboot`, `setRecordAudio`, `setRotationStatus`, `setSpeakerVolume`, `setTargetTrackConfig`, `setTimezone`, `setVoiceEnhancement`, `setWakeUpConfig`.
- Level 3: `setAccountEnabled`, `setChargingMode`, `setCoverConfig`, `setLedStatus`, `setLensMaskConfig`, `setUpnpInfo`.
- Level 4: `configRestore`, `reset`, `setFirmwareAutoUpgradeConfig`, `setThirdPartyCompatibility`, `softReset`, `startFirmwareUpgrade`.

## Follow-ups

- A parameter-aware rule in `policy.py` and `calls.py` would let privacy ON drop to level 1. The comment above `setLensMaskConfig` in the area module lists what it needs.

## What was checked on real devices

On 2026-10-12, on a C225, a C465, a C610 and a D230: the reads of the image, audio, battery, system and privacy settings,
with the results noted in the sections above. The microphone volume of the C225 was changed and set back
(`camera_audio_set`). Camera movement, reboot, reset, firmware, UPnP, the compatibility switch and the other writes were
not tried.
