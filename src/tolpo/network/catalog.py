"""App-derived host purposes. These are hypotheses about devices, not measurements."""

from fnmatch import fnmatchcase

from .models import hostname, ip, NetworkError

# The audit's Part 1.1, in our own words. Specific names precede broad regional families.
CATALOG = (
    ("n-wap.i.tplinkcloud.com", "account and device API"),
    ("account-api.i.tplinkcloud.com", "account API"),
    ("account-captcha.tplinkcloud.com", "account captcha"),
    ("account-delete.tplinkcloud.com", "account deletion"),
    ("oauth.tplinkcloud.com", "linked service authorization"),
    ("n-oauth.tplinkcloud.com", "linked service authorization"),
    ("api.i.tplinknbu.com", "IoT cloud API"),
    ("app-server.iot.i.tplinknbu.com", "service address lookup"),
    ("cloud-config-api.i.tplinknbu.com", "app configuration"),
    ("ota.i.tplinkcloud.com", "firmware offers and updates"),
    ("tapo.tplinkcloud.com", "Tapo web pages"),
    ("stun.tplinkcloud.com", "camera peer-to-peer setup"),
    ("*-cipc-api.i.tplinkcloud.com", "remote camera relay API"),
    ("*-relay-dcipc.i.tplinkcloud.com", "remote camera relay"),
    ("n-wap-gw.tplinkcloud.com", "camera gateway fallback"),
    ("n-wap-dcipc.tplinkcloud.com", "camera media cloud"),
    ("analytics.tplinkcloud.com", "media telemetry"),
    ("n-da.tplinkcloud.com", "app telemetry"),
    ("alexa-ffs.tplinknbu.com", "Alexa integration"),
    ("api-aog-smart-home.tplinknbu.com", "Google Assistant integration"),
    ("ifttt-iot-adapter.tplinknbu.com", "IFTTT integration"),
    ("api.tplinkra.com", "legacy Kasa cloud"),
    ("assets.tplinkra.com", "legacy locale assets"),
    ("deventry.tplinkcloud.com", "legacy device cloud"),
    ("n-deventry.tplinkcloud.com", "legacy device cloud"),
    ("ipcserv.tplinkcloud.com", "legacy camera cloud"),
    ("community.tp-link.com", "support community"),
    ("livechat.tp-link.com", "support chat"),
    ("myticket.tp-link.com", "support tickets"),
    ("privacy.tp-link.com", "privacy pages"),
    ("tplink.channel.io", "support chat"),
    ("*.store.tapo.com", "store"),
    ("www.tapo.com", "product pages"),
    ("dns.google", "phone relay DNS-over-HTTPS fallback"),
    ("8.8.8.8", "Google DNS and phone relay fallback"),
    ("8.8.4.4", "Google DNS and phone relay fallback"),
    # UNVERIFIED: FCM endpoints are SDK-derived; the audit did not measure them.
    ("fcm.googleapis.com", "phone push notifications (UNVERIFIED SDK host)"),
    ("app-measurement.com", "phone analytics"),
    ("firebase-settings.crashlytics.com", "phone crash reporting"),
    ("tapo-android.firebaseio.com", "phone Firebase project"),
    ("maps.googleapis.com", "phone places lookup"),
    ("uat2-iot-as-aps1.s3.ap-southeast-1.amazonaws.com", "test robot map bucket"),
    ("*.tplinkcloud.com", "regional TP-Link cloud service"),
    ("*.tplinknbu.com", "regional IoT, events, video or AI service"),
)


def explain(value: str) -> dict:
    try:
        value = ip(value)
    except NetworkError:
        value = hostname(value)
    for pattern, purpose in CATALOG:
        if fnmatchcase(value, pattern):
            return {"destination": value, "family": pattern, "purpose": purpose,
                    "source": "app-derived", "device_use": "unmeasured"}
    return {"destination": value, "purpose": "unknown", "source": "none"}


def dependency(kind: str) -> dict:
    lost = ["remote control", "voice assistants", "cloud smart actions", "firmware offers"]
    kept = ["local control", "stored schedules (verify per model)"]
    if kind in {"camera", "doorbell"}:
        lost += ["remote view", "ring and motion push", "Tapo Care clips", "cloud AI"]
        kept += ["LAN view", "local recording", "on-device detection"]
    if kind in {"hub", "sensor"}:
        lost += ["sensor push alerts"]
    if kind == "vacuum":
        lost += ["cloud map history", "downloaded voice packs"]
    return {"source": "app-derived", "cloud_required": lost, "local_expected": kept,
            "limits": "Device destinations and long outages need measurement. 4G and NVR PoE clients may be outside this router."}
