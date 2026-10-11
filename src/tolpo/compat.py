"""Small patches for Tapo devices that python-kasa does not handle yet."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

ECDSA_SUITES = ("ECDHE-ECDSA-AES128-GCM-SHA256", "ECDHE-ECDSA-AES256-GCM-SHA384")


def allow_ecdsa_cameras() -> bool:
    """Let python-kasa talk to cameras whose TLS certificate uses ECDSA, such as the C465.

    python-kasa offers only RSA cipher suites (the list the Android app uses), so these cameras and
    the client share no suite and the handshake dies with a misleading `[SSL] ASN1 lib` error.
    Offering two ECDSA suites as well changes nothing for devices that use RSA. Certificates are
    already not verified, because the devices present self-signed ones.
    """
    try:
        from kasa.transports.sslaestransport import SslAesTransport

        present = SslAesTransport.CIPHERS.split(":")
    except (ImportError, AttributeError):
        log.warning("python-kasa changed its TLS transport; the ECDSA camera patch was not applied")
        return False
    missing = [s for s in ECDSA_SUITES if s not in present]
    if missing:
        SslAesTransport.CIPHERS = ":".join([*present, *missing])
    return True
