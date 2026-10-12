# The home plan of the 3D view

`tolpo serve` shows the house in 3D at `http://127.0.0.1:8765/home`: the rooms, every device in its room, its state,
controls, and live video from the cameras the owner enabled. What the view draws comes from a floor plan, and these
tools let an agent build that plan with the person: draft it from the devices, ask about the real rooms, adjust it, and
save it. The plan is the owner's own file (`server.home_layout`, by default `~/.config/tolpo/home.json`). It stays on
the machine and is never shipped with tolpo. No device is contacted by these tools.

- `home_plan_get`: Read the saved plan, the devices it places and does not place, the view address and the plan format.
- `home_plan_draft`: Draft a plan from the devices tolpo knows. Rooms are guessed from words in device names (English,
  German and Spanish), door and window sensors go on walls, cameras and the doorbell go outside. Saves nothing.
- `home_plan_set`: Replace the plan after checking every value. Level 1. An invalid plan is refused and the file is kept.
- `home_view_url`: The address of the 3D view, and that `tolpo serve` must run for it.

## The plan format

```json
{"version": 1,
 "rooms": [{"id": "office", "name": "Office", "x": -7, "z": -5, "w": 5, "d": 5},
           {"id": "backyard", "name": "Backyard", "x": -9, "z": -12, "w": 21, "d": 7, "outside": true}],
 "devices": {"office-1": {"room": "office", "x": -5.75, "z": -3.75},
             "back-door": {"room": "office", "x": -4.5, "z": -5},
             "garden-cam": {"room": "backyard", "x": 1.5, "z": -5.6, "yaw": 0}}}
```

Distances are metres on the floor. `x` grows to the right and `z` toward the front of the house; `x` and `z` of a room
are its back-left corner, `w` and `d` its size. A door or window sensor placed exactly on a wall line gets a door, a
window or a garage door in the view, which opens when the sensor reports open. `yaw` turns a camera or a doorbell:
0 faces the back, 180 the front. Rooms without a device are not drawn. Names are cleaned and cut to 48 characters,
ids are short lower-case words, and numbers must stay within 200 m of the origin.

## A good conversation

1. Call `home_plan_draft` and describe the rooms it guessed.
2. Ask which rooms the house really has, roughly how big they are, and where any device the draft put in `Other` is.
3. Change the plan, call `home_plan_set`, and give the person the address from `home_view_url`.
4. Later corrections can be made by dragging devices in the view (Edit layout, then Save layout) or with another
   `home_plan_set`.

## What was checked

The tests cover the draft (every device placed, sensors on walls, nothing saved), the round trip through the owner's
file (mode 0600), refusals that keep the file, level 0 reading but not saving, and room words in three languages.
The 3D view and its plan were tried on the owner's home on 2026-10-12: nine rooms and 18 devices, live video from a
wired C225 and a battery C465, a bulb switched off and on, and the plan dragged and saved.
