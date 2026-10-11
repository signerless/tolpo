# Camera PTZ and settings

These tools use the owner's existing camera session. Each call reads the component list first.
Unsupported features are refused. Writes refresh device state. Movement is level 1.
Viewpoints, patrol and automatic tracking are level 2.

- `camera_ptz_status`: Read viewpoints, angle ranges, patrol points, patrol status or tracking.
- `camera_ptz_stop`: Stop manual movement.
- `camera_ptz_cruise`: Sweep once horizontally or vertically, or stop the sweep.
- `camera_ptz_calibrate`: Calibrate the motor and resume at the factory or saved position.
- `camera_ptz_save_preset`: Save the current viewpoint with a plain name.
- `camera_ptz_delete_presets`: Delete selected existing viewpoints.
- `camera_ptz_patrol`: Start or stop the configured patrol.
- `camera_ptz_patrol_point`: Add, edit or delete one patrol point and its wait time.
- `camera_ptz_auto_tracking`: Enable or disable basic target tracking.

Tests use fakes and sanitized responses from C225, C465, C610 and the hub-managed D230 doorbell.
Check all requests on a PTZ camera, especially patrol point ids and response wrappers.
Absolute movement is omitted: the audit names `absoluteMove` but does not resolve its module and section.
Patrol schedules, ordering, attributes and per-target tracking are deferred until their capability and payload variants are resolved.

Image writes are level 2. Model-reported ranges gate night vision, flip and mirror, style and HDR.
OSD preserves position and time format. Multi-lens image writes and battery OSD are refused.

- `camera_image_read`: Read image settings, privacy regions, overlays and supported ranges.
- `camera_image_mode`: Set night vision, flip or mirror, or image style within supported values.
- `camera_image_hdr`: Enable or disable main-stream HDR within supported values.
- `camera_image_osd`: Show or hide time, name or logo, and change plain name text.
- `camera_image_privacy_zones`: Enable or disable existing blacked-out regions.
- `camera_image_led`: Enable or disable a version 1 indicator light.
- `camera_system_privacy_status`: Read the lens mask state.
- `camera_system_privacy`: Close or open the lens mask with level 3 confirmation.

Privacy OFF requires level 3 and `setLensMaskConfig <device id>` confirmation.
Privacy ON also requires level 3 for now. Shared `calls.py` and `policy.py` need parameter-aware
rules to allow ON at level 1 without also allowing OFF. Neither shared file was changed.
The audit found no camera privacy schedule. There is no invented schedule method.
Privacy-region editing, LED color and version 2 LED writes are deferred because model-specific ranges
and payload variants are unresolved. Existing regions can be read and toggled.
Check image capability ranges, HDR main-stream values, OSD table layout and lens-mask write effects on a camera.
Doorbell image support needs separate real-device checks. All tests use fakes.

- `camera_battery_read`: Read status, charging, modes, capability ranges, PIR sensitivity or wake-up settings.
- `camera_battery_mode`: Set a reported power source or working mode at level 2.
- `camera_battery_clips`: Set clip length, buffer and retrigger time at level 2.
- `camera_battery_wake_up`: Set a detection or doorbell wake trigger at level 2.
- `camera_battery_charging_privacy`: Change supported charging privacy with level 3 confirmation.
- `camera_battery_statistics`: Read up to 31 days of compact battery and solar usage.

Battery module requests support version 3 and later. Clips and wake-up reads support both flat version 1
and nested version 2 dialects. Clip writes select the matching dialect. Version 1 PIR reads use `pir_verify`.
Clip timing accepts local sanity bounds of 0 to 3600 seconds, with a positive clip length.
The camera may enforce a narrower range. The audit did not identify a clip range probe.
Working mode uses `follow_config=true`. Custom force parameters, charging adapter mode and PIR tuning are deferred.
No tool wakes a sleeping camera. A camera that does not answer on the LAN may need BLE hardware.
Check battery capability ranges, working-mode follow behavior, statistics wrappers and charging privacy on a battery
camera and a battery doorbell. All of these requests were tested only with fakes.

- `camera_audio_read`: Read microphone, speaker, audio recording and noise processing states.
- `camera_audio_volume`: Set microphone or speaker volume at level 2. Zero sets the volume to its minimum.
- `camera_audio_record`: Enable or disable recorded microphone audio at level 2.
- `camera_audio_voice_enhancement`: Change supported voice enhancement at level 2.

Microphone writes preserve current codec, sample rate, channels and bitrate.
The local volume sanity bound is 0 to 100. Check both volume endpoints on camera and doorbell hardware.
Noise suppression and echo cancellation are read-only. The audit resolves their fields but not a write method.
Sound playback and talk-back need a media-channel implementation and level 3 confirmation. They are omitted here.

- `camera_system_read`: Read time, Wi-Fi signal, UPnP, local compatibility, existing account status, reboot or firmware state.
- `camera_system_timezone`: Set an IANA time zone and NTP mode at level 2.
- `camera_system_upnp`: Enable or disable router port mapping with level 3 confirmation. Keep its mapping mode.
- `camera_system_compatibility`: Change the local integration flag with level 3 confirmation.
- `camera_system_account_enabled`: Toggle an existing RTSP/ONVIF login with level 3 confirmation. Return no credentials.
- `camera_system_reboot`: Restart the camera at level 2.
- `camera_system_reboot_schedule`: Toggle scheduled restarts or change their HH:MM time at level 2. Keep days and spread.
- `camera_system_firmware_check`: Ask the camera to check the vendor firmware catalogue at level 0.
- `camera_system_firmware_update`: Start firmware installation with level 4 confirmation.
- `camera_system_auto_update`: Change unattended updates with level 4 confirmation. Keep the update window.
- `camera_system_reset`: Factory reset or restore defaults with level 4 confirmation.

Camera accounts cannot be created here. The audit excludes their password encryption because it needs key material.
An existing account can be read for enabled status and toggled. Its username and password never leave the tool.
The compatibility tool changes only the local device flag. Its cloud account setting and certificate operations are omitted.
Wi-Fi reads omit SSID, addresses and MAC. UPnP changes can expose the camera through the router.
Time sync selects NTP mode. The camera sets its own clock. There is no invented manual clock or sync method.
Factory reset uses `softReset` with `type=factory` on system version 4 or battery cameras and `reset` otherwise.
Restore sends `configRestore`. Reset confirmations name the selected method and device id.
Firmware updates use the camera's vendor download path. No image or key is supplied by tolpo.
Reboot, reset and firmware writes attempt a state refresh. An outage during refresh is reported as an error.
Check those commands and post-command connectivity on camera and doorbell hardware before use.
Firmware capability flat responses, auto-update support flags and UPnP mapping modes need hardware checks.
Hub-managed cameras read their capabilities through `getChildDeviceComponentList` on the parent hub.
Clock reads follow the reported `timezone` component. Existing privacy regions follow `blockZone`.
Wi-Fi responses are flat, as captured in all three camera fixtures.
All verification in this area uses fakes. No network host or device was contacted.

UNVERIFIED response assumptions: `getPatrolInfo` is read under `patrol.get_patrol_info`,
`getBatteryStatistic` under `battery.statistic`, and `getFirmwareCapability` as flat `auto_upgrade` fields.
Their requests are resolved from the request builders. Their response nesting needs sanitized hardware fixtures.
Unexpected or rejected responses raise an error with the device and method. Raw device error text is hidden.

Safety checks were broken in scratch copies, with each edit confirmed by `diff`.
Lowered method levels caused 11 failures. Removed confirmation caused 8 failures.
Removed private-field filtering caused 46 failures. Disabled text flattening caused 1 failure.
Disabled text shortening caused 46 failures. Removed numeric validation caused 1 failure.
Removed name validation caused 2 failures. Removed supported-value checks caused 3 failures.
Removed component checks, battery-version checks, nested error checks and error-text filtering each caused 1 failure.
Removed state refresh caused 11 failures. Scratch copies were deleted. Shared files were not changed.

The prescribed sandbox suite also includes one existing localhost socket test in `tests/test_snapshot.py`.
`test_real_ffmpeg_failure_names_no_password` expects a refused connection but sandbox denial yields unreachable.
It is excluded from the final offline run along with `tests/test_media.py` and `tests/test_pictures.py`.
Packaging follow-up: shared `package.json` currently includes `src/tolpo/*.py` but not area modules.
Add `src/tolpo/areas/*.py` to its files list before packaging this area.

Removing the existing-login check caused 1 failure. That scratch copy was also deleted.

Declared device methods:

- Level 0: `checkFirmwareVersionByCloud`, `getAudioConfig`, `getBatteryCapability`, `getBatteryOperatingMode`, `getBatteryStatistic`, `getBatteryStatus`, `getChargingMode`, `getChargingPrivacyMode`, `getChildDeviceComponentList`, `getClipsConfig`, `getClockStatus`, `getCloudConfig`, `getConnectionType`, `getCoverConfig`, `getCoverRegion`, `getFirmwareAutoUpgradeConfig`, `getFirmwareCapability`, `getFirmwareUpdateStatus`, `getImageCapability`, `getImageStyleCapability`, `getImageStyleConfig`, `getLedStatus`, `getLensMaskConfig`, `getMotorAngleRange`, `getNightVisionCapability`, `getNightVisionModeConfig`, `getOsd`, `getOsdCapability`, `getPatrolInfo`, `getPatrolStatus`, `getPirSensitivity`, `getPowerMode`, `getPresetConfig`, `getReboot`, `getRotationStatus`, `getTargetTrackConfig`, `getThirdAccount`, `getThirdPartyCompatibility`, `getTimezone`, `getUpnpInfo`, `getVideoCapability`, `getVoiceEnhancementConfig`, `getWakeUpConfig`.
- Level 1: `cruiseMove`, `cruiseStop`, `manualCalibrate`, `stopMove`.
- Level 2: `addMotorPostion`, `addPatrol`, `deletePatrol`, `deletePreset`, `editPatrol`, `rebootDevice`, `setBatteryOperatingMode`, `setClipsConfig`, `setCoverConfig`, `setHDR`, `setImageStyleConfig`, `setLedStatus`, `setMicrophoneVolume`, `setNightVisionModeConfig`, `setOsd`, `setPatrolStatus`, `setPowerMode`, `setReboot`, `setRecordAudio`, `setRotationStatus`, `setSpeakerVolume`, `setTargetTrackConfig`, `setTimezone`, `setVoiceEnhancement`, `setWakeUpConfig`.
- Level 3: `setAccountEnabled`, `setChargingPrivacyMode`, `setLensMaskConfig`, `setThirdPartyCompatibility`, `setUpnpInfo`.
- Level 4: `configRestore`, `reset`, `setFirmwareAutoUpgradeConfig`, `softReset`, `startFirmwareUpgrade`.
