"""HTTP surface. Small on purpose: list, inspect, switch, dim, and a guarded generic setter."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, JsonValue
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import Config
from .registry import Registry, RegistryError
from .views import BatchItem, DeviceDetail, DeviceSummary, detail, run_batch, summary

DESCRIPTION = """
Local control of smart-home devices for AI agents.

Refer to a device by its short `id` (`office-1`), by its name as shown in the app
(case-insensitive), or by a glob (`office*`) in the batch endpoints.

Reads are broad; writes are limited to power, brightness and color unless the owner
widens `policy.allow_write`. Responses never include Matter setup codes or the Wi-Fi name.
A still picture is available only from cameras the owner listed in `cameras.snapshots`.
"""


class PowerBody(BaseModel):
    on: bool


class LightBody(BaseModel):
    on: bool | None = Field(None, description="Also switch the light on (true) or off (false)")
    brightness: int | None = Field(None, ge=1, le=100)
    color_temp: int | None = Field(None, ge=1000, le=12000, description="Kelvin; white light")
    hue: int | None = Field(None, ge=0, le=360)
    saturation: int | None = Field(None, ge=0, le=100)


class FeatureBody(BaseModel):
    value: JsonValue = None


class BatchPower(PowerBody):
    targets: list[str] = Field(min_length=1, examples=[["office*", "Desk Lamp"]])


class BatchLight(LightBody):
    targets: list[str] = Field(min_length=1, examples=[["office*"]])


def create_app(
    cfg: Config,
    registry: Registry | None = None,
    *,
    discover: bool = True,
    allowed_hosts: list[str] | None = None,
) -> FastAPI:
    reg = registry or Registry(cfg)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await reg.start(discover=discover)
        yield
        await reg.stop()

    app = FastAPI(title="tolpo", version=__version__, description=DESCRIPTION, lifespan=lifespan)

    if not cfg.token:
        # Without a token the only protection is the loopback bind, so also refuse requests
        # whose Host header is not local (blocks DNS-rebinding from a web page).
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or ["localhost", "127.0.0.1"])

    async def require_token(authorization: str | None = Header(default=None)) -> None:
        if not cfg.token:
            return
        scheme, _, supplied = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(supplied.encode(), cfg.token.encode()):
            raise HTTPException(401, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(require_token)]

    @app.exception_handler(RegistryError)
    async def on_registry_error(_, exc: RegistryError):
        return JSONResponse(status_code=exc.status_code, content={"error": str(exc), "candidates": exc.candidates})

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__}

    @app.get("/status", dependencies=auth, summary="Discovery status and devices that are offline")
    async def status():
        return reg.health()

    @app.post("/scan", dependencies=auth, summary="Search the network for new or moved devices")
    async def scan():
        await reg.discover()
        return reg.health()

    @app.get("/devices", response_model=list[DeviceSummary], dependencies=auth, summary="List devices")
    async def list_devices(kind: str | None = None, q: str | None = None, fresh: bool = False):
        """Filter by `kind` (light, plug, sensor, camera...) or by text in the name with `q`.
        State is cached for a few seconds; pass `fresh=true` to ask the devices again."""
        items = reg.entries(kind=kind, q=q)
        await reg.refresh_all(items, force=fresh)
        return [summary(reg, e) for e in items]

    @app.get("/devices/{ref}", response_model=DeviceDetail, dependencies=auth, summary="Full detail of one device")
    async def get_device(ref: str):
        entry = reg.resolve_one(ref)
        await reg.refresh(entry)
        return detail(reg, entry)

    @app.get(
        "/devices/{ref}/snapshot",
        dependencies=auth,
        response_class=Response,
        responses={200: {"content": {"image/jpeg": {}}, "description": "One JPEG picture, taken now"}},
        summary="One still picture from a camera",
    )
    async def snapshot(ref: str, quality: Literal["low", "high"] = "low"):
        """Only cameras the owner enabled. `low` is the camera's small stream, `high` its main stream
        (scaled to at most 1280 pixels wide). The picture is never stored or cached."""
        picture = await reg.snapshot(reg.resolve_one(ref), quality)
        return Response(
            picture.data,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store",
                "Cross-Origin-Resource-Policy": "same-origin",  # another web page cannot embed it
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/devices/{ref}/power", response_model=DeviceSummary, dependencies=auth, summary="Switch on or off")
    async def power(ref: str, body: PowerBody):
        entry = reg.resolve_one(ref)
        await reg.set_power(entry, body.on)
        return summary(reg, entry)

    @app.post("/devices/{ref}/toggle", response_model=DeviceSummary, dependencies=auth, summary="Flip the power state")
    async def toggle(ref: str):
        entry = reg.resolve_one(ref)
        await reg.toggle(entry)
        return summary(reg, entry)

    @app.post("/devices/{ref}/light", response_model=DeviceSummary, dependencies=auth, summary="Brightness and color")
    async def light(ref: str, body: LightBody):
        entry = reg.resolve_one(ref)
        await reg.set_light(entry, **body.model_dump())
        return summary(reg, entry)

    @app.put(
        "/devices/{ref}/features/{feature_id}",
        response_model=DeviceSummary,
        dependencies=auth,
        summary="Set any feature the policy allows",
    )
    async def put_feature(ref: str, feature_id: str, body: FeatureBody):
        entry = reg.resolve_one(ref)
        await reg.set_feature(entry, feature_id, body.value)
        return summary(reg, entry)

    @app.post("/batch/power", response_model=list[BatchItem], dependencies=auth, summary="Switch several devices")
    async def batch_power(body: BatchPower):
        return await run_batch(reg, body.targets, lambda e: reg.set_power(e, body.on))

    @app.post("/batch/light", response_model=list[BatchItem], dependencies=auth, summary="Set several lights")
    async def batch_light(body: BatchLight):
        settings = body.model_dump(exclude={"targets"})
        return await run_batch(reg, body.targets, lambda e: reg.set_light(e, **settings))

    return app

