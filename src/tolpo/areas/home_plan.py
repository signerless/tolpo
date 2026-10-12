"""MCP tools for the 3D home view: an agent drafts, reads and saves the floor plan that `tolpo serve` shows at /home.

The plan is the owner's own file (`server.home_layout`, by default ~/.config/tolpo/home.json). It never leaves the
machine and never ships with tolpo. No device is contacted: these tools read what discovery already knows.
"""

# Do NOT add `from __future__ import annotations` here: the MCP SDK reads the tool signatures at runtime.
import math
import re
from typing import Annotated, Any

from pydantic import Field

from .. import home
from ..policy import Tier
from ..registry import Forbidden, RegistryError

METHODS = []  # no device method: the plan is tolpo's own file

PLAN_FORMAT = """\
A plan is {"version": 1, "rooms": [...], "devices": {...}}. Distances are metres on the floor: x grows to the right,
z grows toward the front of the house. A room is {"id": "office", "name": "Office", "x": -7, "z": -5, "w": 5, "d": 5},
where x and z are its back-left corner and w and d its size; add "outside": true for a garden, yard or street.
A device is placed by its short id: {"office-1": {"room": "office", "x": -5.75, "z": -3.75}}. Put a door or window
sensor exactly on a wall line (for example z equal to the room's z + d for its front wall): the view then draws a door
or a window there that opens when the sensor says open. A camera or doorbell can take "yaw" in degrees to turn it:
0 faces the back (-z), 180 faces the front. Rooms without any device are not drawn."""

# Words in device names that tell the room, in English, German and Spanish. The first match wins.
ROOM_WORDS = [
    ("office", "Office", r"office|b[uü]ro|oficina|study|desk"),
    ("kitchen", "Kitchen", r"kitchen|k[uü]che|cocina"),
    ("kids", "Kids room", r"kid|kinder|child|ni[nñ]o|nursery"),
    ("bedroom", "Bedroom", r"bed|schlaf|dormitorio|habitaci"),
    ("bathroom", "Bathroom", r"bath|\bbad\b|ba[nñ]o|wc|shower|dusche|toilet"),
    ("utility", "Utility room", r"heat|heizung|boiler|utility|laundry|wasch|caldera|technik"),
    ("garage", "Garage", r"garage|carport"),
    ("basement", "Basement", r"basement|cellar|keller|s[oó]tano"),
    ("hall", "Hall", r"hall|flur|corridor|pasillo|entr|foyer|main.?door|front.?door|haust[uü]r"),
    ("living", "Living room", r"living|lounge|wohn|sala|sal[oó]n|tv|sofa"),
    ("backyard", "Backyard", r"backyard|garden|garten|patio|terrace|terrasse|jard[ií]n|yard|outdoor|back"),
]
OUTSIDE = {"backyard", "front"}
ORDER = ["office", "living", "kitchen", "bedroom", "kids", "hall", "bathroom", "utility", "basement", "other", "garage"]
NAMES = {key: name for key, name, _ in ROOM_WORDS} | {"front": "Front yard", "other": "Other"}


def _guess_room(entry) -> str:
    text = f"{entry.slug} {entry.device.alias or ''}".lower()
    kind = entry.kind
    if kind == "doorbell":
        return "front"
    for key, _, pattern in ROOM_WORDS:
        if re.search(pattern, text):
            if key == "backyard" and kind not in ("camera", "plug", "light", "sensor"):
                continue
            if key == "backyard" and kind == "sensor":
                return "living"  # a back door or window sensor sits on the house, not in the garden
            return key
    features = entry.device.features
    if kind == "camera":
        return "backyard"
    if "water_alert" in features or "water_leak" in features:
        return "bathroom"
    if kind in ("hub", "plug") or "temperature" in features:
        return "living"
    return "other"


def _opening(entry) -> str | None:
    """door, window or garage for a contact sensor, None for anything else."""
    if "is_open" not in entry.device.features:
        return None
    text = f"{entry.slug} {entry.device.alias or ''}".lower()
    if re.search(r"window|fenster|ventana", text):
        return "window"
    if re.search(r"garage", text):
        return "garage"
    return "door"


def draft(registry) -> dict[str, Any]:
    """A first plan from what discovery knows. Rooms come from the words in device names, sizes from how many devices
    a room holds, door and window sensors go on walls, cameras and the doorbell go outside."""
    groups: dict[str, list] = {}
    for entry in sorted(registry.entries(), key=lambda e: e.slug):
        groups.setdefault(_guess_room(entry), []).append(entry)

    inside = [k for k in ORDER if k in groups]
    rooms, x, z, row_depth, row_width = [], 0.0, 0.0, 0.0, 16.0
    spots: dict[str, dict] = {}
    for key in inside:
        count = len(groups[key])
        w = 4.0 if count <= 2 else 5.0
        d = 4.0 if count <= 2 else 5.0
        if key == "garage":
            w, d = 5.0, 5.0
        if x + w > row_width and x > 0:
            x, z, row_depth = 0.0, z + row_depth, 0.0
        rooms.append({"id": key, "name": NAMES[key], "x": x, "z": z, "w": w, "d": d})
        x += w
        row_depth = max(row_depth, d)
    depth = z + row_depth
    width = max([r["x"] + r["w"] for r in rooms] or [8.0])
    # centre the house on the origin
    shift_x, shift_z = -width / 2, -depth / 2
    for r in rooms:
        r["x"], r["z"] = round(r["x"] + shift_x, 2), round(r["z"] + shift_z, 2)
    front, back = round(depth / 2, 2), round(-depth / 2, 2)

    for room in rooms:
        members = groups[room["id"]]
        walls = [e for e in members if _opening(e)]
        free = [e for e in members if not _opening(e)]
        cols = max(1, math.ceil(math.sqrt(len(free))))
        rows = max(1, math.ceil(len(free) / cols))
        for i, entry in enumerate(free):
            cx = room["x"] + room["w"] * ((i % cols) + 0.5) / cols
            cz = room["z"] + room["d"] * ((i // cols) + 0.5) / rows
            spots[entry.slug] = {"room": room["id"], "x": round(cx, 2), "z": round(cz, 2)}
        for i, entry in enumerate(walls):
            kind = _opening(entry)
            text = f"{entry.slug} {entry.device.alias or ''}".lower()
            along = room["x"] + room["w"] * (i + 1) / (len(walls) + 1)
            if kind == "window":
                spots[entry.slug] = {"room": room["id"], "x": room["x"], "z": round(room["z"] + room["d"] / 2, 2)}
            elif kind == "garage" or re.search(r"main|front|haust|entr|principal", text) or room["id"] == "hall":
                spots[entry.slug] = {"room": room["id"], "x": round(along, 2), "z": room["z"] + room["d"]}
            else:
                spots[entry.slug] = {"room": room["id"], "x": round(along, 2), "z": room["z"]}

    for key, z_from, z_size, yaw in (("backyard", back - 7, 7, 0), ("front", front, 4, 180)):
        members = groups.get(key, [])
        if not members:
            continue
        rooms.append({"id": key, "name": NAMES[key], "x": round(-width / 2 - 2, 2), "z": round(z_from, 2), "w": round(width + 4, 2),
                      "d": z_size, "outside": True})
        for i, entry in enumerate(members):
            px = -width / 2 + width * (i + 1) / (len(members) + 1)
            pz = back - 0.6 if key == "backyard" else front + 0.4
            spots[entry.slug] = {"room": key, "x": round(px, 2), "z": round(pz, 2), "yaw": yaw}

    plan = home.validate({"version": 1, "rooms": rooms, "devices": spots})
    guesses = {room["name"]: sorted(spots_for(plan, room["id"])) for room in plan["rooms"]}
    return {
        "plan": plan,
        "rooms_guessed": guesses,
        "next": "This is a guess from device names. Show it to the person, ask which rooms they really have and where "
                "each device is, change the plan, then save it with home_plan_set.",
        "format": PLAN_FORMAT,
    }


def spots_for(plan, room_id):
    return [slug for slug, spot in plan["devices"].items() if spot.get("room") == room_id]


def waiting_names(registry) -> list[str]:
    """Names of known devices that are still connecting. Names only: addresses are not for the agent."""
    return sorted(str(d.get("name") or "") for d in registry.not_connected())


def view_url(cfg) -> str:
    host = cfg.host if cfg.host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    host = f"[{host}]" if ":" in host else host
    return f"http://{host}:{cfg.port}/home"


Plan = Annotated[dict[str, Any], Field(description="The whole plan: {\"version\": 1, \"rooms\": [...], \"devices\": {...}}. "
                                                    "See home_plan_get for the format.")]


def tools(ctx):
    def known(plan):
        slugs = {e.slug for e in ctx.registry.entries()}
        placed = set(plan["devices"])
        return sorted(placed - slugs), sorted(slugs - placed)

    @ctx.tool(title="Home plan")
    async def home_plan_get() -> dict[str, Any]:
        """Read the floor plan of the 3D home view, which devices it places and which it does not, and the plan format.
        Nothing is contacted."""
        await ctx.ready_for(["*"])
        with ctx.translated():
            try:
                plan = home.load(ctx.registry.cfg.home_layout) or home.empty()
            except home.LayoutError as exc:
                raise RegistryError(f"the saved plan is not valid: {exc}") from None
            unknown, unplaced = known(plan)
            return {"plan": plan, "unplaced_devices": unplaced, "placed_but_not_found": unknown,
                    "still_connecting": waiting_names(ctx.registry), "view": view_url(ctx.registry.cfg), "format": PLAN_FORMAT}

    @ctx.tool(title="Draft a home plan")
    async def home_plan_draft() -> dict[str, Any]:
        """Draft a floor plan from the devices tolpo knows: rooms guessed from their names, door and window sensors on
        walls, cameras and the doorbell outside. Saves nothing. Show it to the person and ask about their real rooms
        before saving it with home_plan_set."""
        await ctx.ready_for(["*"])
        with ctx.translated():
            out = draft(ctx.registry)
            waiting = waiting_names(ctx.registry)
            if waiting:
                out["still_connecting"] = waiting
                out["next"] = (f"{len(waiting)} known device(s) are still connecting and are not in this draft yet. Draft again "
                               "in a moment. ") + out["next"]
            return out

    @ctx.tool(title="Save the home plan", tier=Tier.CONTROL)
    async def home_plan_set(plan: Plan) -> dict[str, Any]:
        """Replace the floor plan of the 3D home view. Every value is checked before the owner's file is written; an
        invalid plan is refused and the file stays as it was. Level 1. No device is changed."""
        with ctx.translated():
            if ctx.registry.cfg.tier < Tier.CONTROL:
                raise Forbidden("Saving the home plan is a level 1 action and the owner's policy blocks it.")
            try:
                saved = home.save(ctx.registry.cfg.home_layout, plan)
            except home.LayoutError as exc:
                raise RegistryError(f"the plan was not saved: {exc}") from None
            unknown, unplaced = known(saved)
            return {"saved": True, "rooms": len(saved["rooms"]), "devices": len(saved["devices"]),
                    "unplaced_devices": unplaced, "placed_but_not_found": unknown, "view": view_url(ctx.registry.cfg)}

    @ctx.tool(title="Where to see the house in 3D")
    async def home_view_url() -> dict[str, Any]:
        """The address of the 3D home view. It is served by `tolpo serve` (or `npx -y tolpo serve`), not by the MCP
        server, so the person may need to start it first. It shows the plan, the state of every device, controls, and
        live video from the cameras the owner enabled."""
        return {"url": view_url(ctx.registry.cfg),
                "how": "Run `tolpo serve` (or `npx -y tolpo serve`) on the same machine, then open the address in a browser."}
