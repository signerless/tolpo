// The 3D home view of tolpo. It draws the owner's floor plan (GET /home/layout), puts every device from GET /devices in
// its room, and lets the owner read and control a device by clicking it, and watch a camera live. It talks to this
// server only, and every text that comes from a device is written with textContent, never as markup.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/addons/renderers/CSS2DRenderer.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

const T = {
  updated: (t) => `Updated ${t}`,
  needToken: "The access token is needed",
  notPlaced: "Not placed",
  kinds: { light: "Light", plug: "Plug", switch: "Switch", sensor: "Sensor", hub: "Hub", camera: "Camera",
           doorbell: "Doorbell", vacuum: "Robot vacuum", lock: "Lock" },
  readings: { is_open: "Open", temperature: "Temperature", humidity: "Humidity", water_alert: "Water detected",
              water_leak: "Water detected", current_consumption: "Power", voltage: "Voltage", current: "Current",
              battery_low: "Battery low", motion_detected: "Motion", signal_level: "Signal", rssi: "Wi-Fi signal",
              overheated: "Overheated", cloud_connection: "Cloud connection" },
  batteryNote: "Battery camera: live video keeps it awake and uses battery, so it stops after two minutes.",
  saved: "Layout saved",
  editHint: "Drag a device to move it, then save the layout.",
};

// ---- icons, built as SVG nodes (no markup strings) -----------------------------------------------------------------
const NS = "http://www.w3.org/2000/svg";
const ICONS = {
  bulb: [["path", { d: "M9 18h6" }], ["path", { d: "M10 21h4" }],
         ["path", { d: "M12 3a6 6 0 0 0-3.6 10.8c.7.5 1.1 1.3 1.1 2.1V16h5v-.1c0-.8.4-1.6 1.1-2.1A6 6 0 0 0 12 3z" }]],
  plug: [["path", { d: "M9 3v5" }], ["path", { d: "M15 3v5" }], ["path", { d: "M6 8h12v3a6 6 0 0 1-12 0z" }], ["path", { d: "M12 17v4" }]],
  lamp: [["path", { d: "M8 3h8l3.5 7h-15z" }], ["path", { d: "M12 10v10" }], ["path", { d: "M8 21h8" }]],
  camera: [["rect", { x: 2.5, y: 7, width: 13, height: 10, rx: 2 }], ["path", { d: "M15.5 11l6-3.5v9l-6-3.5" }]],
  door: [["path", { d: "M6 21V4.5A1.5 1.5 0 0 1 7.5 3h9A1.5 1.5 0 0 1 18 4.5V21" }], ["path", { d: "M3.5 21h17" }],
         ["circle", { cx: 14.5, cy: 12.5, r: 0.9, fill: "currentColor" }]],
  window: [["rect", { x: 4, y: 3, width: 16, height: 18, rx: 1.5 }], ["path", { d: "M12 3v18" }], ["path", { d: "M4 12h16" }]],
  garage: [["path", { d: "M3 20V9l9-6 9 6v11" }], ["path", { d: "M7 20v-8h10v8" }], ["path", { d: "M7 15.5h10" }]],
  thermo: [["path", { d: "M12 3a2.2 2.2 0 0 0-2.2 2.2v9.1a4 4 0 1 0 4.4 0V5.2A2.2 2.2 0 0 0 12 3z" }], ["path", { d: "M12 9v7.5" }]],
  drop: [["path", { d: "M12 3.2s6.3 6.6 6.3 11.2a6.3 6.3 0 0 1-12.6 0C5.7 9.8 12 3.2 12 3.2z" }]],
  hub: [["rect", { x: 3, y: 12.5, width: 18, height: 7, rx: 2 }], ["path", { d: "M7 16h.01" }], ["path", { d: "M10.5 16h.01" }],
        ["path", { d: "M8.5 9.2a5 5 0 0 1 7 0" }], ["path", { d: "M6 6.6a8.5 8.5 0 0 1 12 0" }]],
  bell: [["rect", { x: 7, y: 2.5, width: 10, height: 19, rx: 3 }], ["circle", { cx: 12, cy: 15, r: 2.6 }], ["path", { d: "M10.2 7h3.6" }]],
  dot: [["circle", { cx: 12, cy: 12, r: 4 }]],
  close: [["path", { d: "M6 6l12 12" }], ["path", { d: "M18 6 6 18" }]],
  expand: [["path", { d: "M4 9V4h5" }], ["path", { d: "M20 9V4h-5" }], ["path", { d: "M4 15v5h5" }], ["path", { d: "M20 15v5h-5" }]],
  play: [["path", { d: "M7 4.5v15l12-7.5z", fill: "currentColor" }]],
  pause: [["path", { d: "M8 5v14" }], ["path", { d: "M16 5v14" }]],
  alert: [["path", { d: "M12 3.5 2.5 20h19z" }], ["path", { d: "M12 10v4.5" }], ["path", { d: "M12 17.5h.01" }]],
  check: [["path", { d: "M5 12.5l4.5 4.5L19 7.5" }]],
  offline: [["path", { d: "M3 3l18 18" }], ["path", { d: "M8.5 16.5a5 5 0 0 1 7 0" }], ["path", { d: "M5 12.6a10 10 0 0 1 4.2-2.4" }],
            ["path", { d: "M19 12.6a10 10 0 0 0-3-1.9" }], ["path", { d: "M12 20h.01" }]],
};

function icon(name) {
  const svg = document.createElementNS(NS, "svg");
  for (const [k, v] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.8",
    "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) svg.setAttribute(k, v);
  for (const [tag, attrs] of ICONS[name] || ICONS.dot) {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
    svg.append(node);
  }
  return svg;
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "text") node.textContent = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
    else if (k === "class") node.className = v;
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  node.append(...children.filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

// ---- talking to tolpo -------------------------------------------------------------------------------------------------
let token = sessionStorage.getItem("tolpo.token") || localStorage.getItem("tolpo.token") || "";

function authHeaders() {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function failure(res) {
  let msg = `Error ${res.status}`;
  try {
    const j = await res.json();
    msg = j.error || (typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail)) || msg;
  } catch { /* not JSON */ }
  return new Error(msg);
}

async function api(path, { method = "GET", body } = {}) {
  const headers = authHeaders();
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new Error("tolpo is not reachable. Is the server running?");
  }
  if (res.status === 401) {
    document.getElementById("token").hidden = false;
    document.getElementById("token-input").focus();
    throw new Error(T.needToken);
  }
  if (!res.ok) throw await failure(res);
  const type = res.headers.get("content-type") || "";
  return type.includes("application/json") ? res.json() : res.blob();
}

document.getElementById("token-ok").addEventListener("click", () => {
  token = document.getElementById("token-input").value.trim();
  sessionStorage.setItem("tolpo.token", token);
  if (document.getElementById("token-remember").checked) localStorage.setItem("tolpo.token", token);
  document.getElementById("token").hidden = true;
  start();
});

let toastTimer = null;
function toast(text, error = false) {
  const box = document.getElementById("toast");
  box.textContent = text;
  box.className = error ? "toast error" : "toast";
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, error ? 6000 : 2600);
}

// ---- renderer, camera, light ------------------------------------------------------------------------------------------
const holder = document.getElementById("scene");
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.0;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
holder.append(renderer.domElement);

const labelRenderer = new CSS2DRenderer();
labelRenderer.domElement.className = "labels";
holder.append(labelRenderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0xe9edf2);
const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 600);
camera.position.set(6, 30, 30);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.maxPolarAngle = 1.22;
controls.minDistance = 6;
controls.maxDistance = 110;

scene.add(new THREE.HemisphereLight(0xffffff, 0xc7d0db, 0.55));
const sun = new THREE.DirectionalLight(0xfff6ea, 1.7);
sun.position.set(14, 26, 18);
sun.castShadow = true;
sun.shadow.mapSize.set(2048, 2048);
sun.shadow.bias = -0.0004;
sun.shadow.normalBias = 0.03;
Object.assign(sun.shadow.camera, { left: -30, right: 30, top: 30, bottom: -30, near: 1, far: 90 });
scene.add(sun);

const ground = new THREE.Mesh(new THREE.PlaneGeometry(400, 400), new THREE.MeshStandardMaterial({ color: 0xe1e6ec, roughness: 1 }));
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
scene.add(ground);

// ---- procedural materials ---------------------------------------------------------------------------------------------
function rng(seed) {
  return () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function canvasTexture(draw, size = 512) {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  draw(canvas.getContext("2d"), size);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
  texture.anisotropy = renderer.capabilities.getMaxAnisotropy();
  return texture;
}

const TEXTURES = {
  wood: canvasTexture((g, s) => {
    const r = rng(7);
    const rows = 8, h = s / rows;
    for (let row = 0; row < rows; row++) {
      let x = -r() * s * 0.5;
      while (x < s) {
        const len = s * (0.35 + r() * 0.45);
        const l = 70 + r() * 8, sat = 28 + r() * 10, hue = 30 + r() * 6;
        g.fillStyle = `hsl(${hue}, ${sat}%, ${l}%)`;
        g.fillRect(x, row * h, len, h);
        g.strokeStyle = `hsla(${hue}, 30%, ${l - 14}%, 0.18)`;
        g.lineWidth = 1;
        for (let k = 0; k < 3; k++) {
          const y = row * h + h * (0.2 + r() * 0.6);
          g.beginPath(); g.moveTo(x, y); g.bezierCurveTo(x + len * 0.3, y + 2, x + len * 0.6, y - 2, x + len, y); g.stroke();
        }
        g.fillStyle = "rgba(90, 64, 40, 0.28)";
        g.fillRect(x, row * h, 1.5, h);
        x += len;
      }
      g.fillStyle = "rgba(90, 64, 40, 0.22)";
      g.fillRect(0, row * h, s, 1.5);
    }
  }),
  tile: canvasTexture((g, s) => {
    const r = rng(11);
    const n = 4, t = s / n;
    for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) {
      g.fillStyle = `hsl(210, 10%, ${90 + r() * 3}%)`;
      g.fillRect(i * t, j * t, t, t);
    }
    g.strokeStyle = "rgba(100, 116, 139, 0.35)";
    g.lineWidth = 3;
    for (let k = 0; k <= n; k++) {
      g.beginPath(); g.moveTo(k * t, 0); g.lineTo(k * t, s); g.stroke();
      g.beginPath(); g.moveTo(0, k * t); g.lineTo(s, k * t); g.stroke();
    }
  }),
  stone: canvasTexture((g, s) => {
    const r = rng(23);
    const n = 2, t = s / n;
    for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) {
      g.fillStyle = `hsl(35, 12%, ${82 + r() * 4}%)`;
      g.fillRect(i * t, j * t, t, t);
    }
    for (let k = 0; k < 2600; k++) {
      g.fillStyle = `rgba(80, 70, 60, ${0.03 + r() * 0.04})`;
      g.fillRect(r() * s, r() * s, 2, 2);
    }
    g.strokeStyle = "rgba(90, 80, 70, 0.3)";
    g.lineWidth = 3;
    for (let k = 0; k <= n; k++) {
      g.beginPath(); g.moveTo(k * t, 0); g.lineTo(k * t, s); g.stroke();
      g.beginPath(); g.moveTo(0, k * t); g.lineTo(s, k * t); g.stroke();
    }
  }),
  concrete: canvasTexture((g, s) => {
    const r = rng(31);
    g.fillStyle = "#c7cbd0";
    g.fillRect(0, 0, s, s);
    for (let k = 0; k < 9000; k++) {
      g.fillStyle = r() > 0.5 ? `rgba(255,255,255,${r() * 0.08})` : `rgba(40,45,52,${r() * 0.07})`;
      g.fillRect(r() * s, r() * s, 2, 2);
    }
  }),
  grass: canvasTexture((g, s) => {
    const r = rng(41);
    g.fillStyle = "#c2d6ad";
    g.fillRect(0, 0, s, s);
    for (let k = 0; k < 14000; k++) {
      g.fillStyle = r() > 0.5 ? `rgba(96, 132, 70, ${r() * 0.16})` : `rgba(236, 246, 218, ${r() * 0.14})`;
      g.fillRect(r() * s, r() * s, 2, 3);
    }
  }),
};

function floorKind(room) {
  if (room.outside) return "grass";
  const name = `${room.id} ${room.name}`.toLowerCase();
  if (/bath|wc|shower|utility|laundry|heat|boiler|kitchen|technical/.test(name)) return "tile";
  if (/garage|carport|workshop|cellar|basement/.test(name)) return "concrete";
  if (/hall|entr|corridor|foyer|lobby/.test(name)) return "stone";
  return "wood";
}

function floorMaterial(room) {
  const kind = floorKind(room);
  const texture = TEXTURES[kind].clone();
  const scale = { wood: 2.4, tile: 1.6, stone: 2.4, concrete: 4, grass: 5 }[kind];
  texture.repeat.set(room.w / scale, room.d / scale);
  texture.needsUpdate = true;
  const tint = room.color ? new THREE.Color(room.color) : new THREE.Color(0xffffff);
  return new THREE.MeshStandardMaterial({ map: texture, color: tint, roughness: kind === "tile" ? 0.45 : 0.85, metalness: 0 });
}

function radialTexture(inner, outer) {
  return canvasTexture((g, s) => {
    const grad = g.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
    grad.addColorStop(0, inner);
    grad.addColorStop(1, outer);
    g.fillStyle = grad;
    g.fillRect(0, 0, s, s);
  }, 256);
}
const POOL = radialTexture("rgba(255,255,255,1)", "rgba(255,255,255,0)");
const SOFT_SHADOW = radialTexture("rgba(15,23,42,0.22)", "rgba(15,23,42,0)");

// ---- the plan, the visible rooms and the architecture ----------------------------------------------------------------
let plan = { version: 1, rooms: [], devices: {} };
const WALL_H = 1.35;
const FLOOR_Y = 0.08;
const architecture = new THREE.Group();
scene.add(architecture);
let archSignature = "";
let openings = [];
const devices = new Map(); // id -> item

function roomAt(x, z) {
  const hits = plan.rooms.filter((r) => x >= r.x - 0.01 && x <= r.x + r.w + 0.01 && z >= r.z - 0.01 && z <= r.z + r.d + 0.01);
  hits.sort((a, b) => Number(!!a.outside) - Number(!!b.outside) || a.w * a.d - b.w * b.d);
  return hits[0] || null;
}

function roomOf(id) {
  const spot = plan.devices[id];
  if (spot && spot.room) return plan.rooms.find((r) => r.id === spot.room) || null;
  const item = devices.get(id);
  return item ? roomAt(item.group.position.x, item.group.position.z) : null;
}

function visibleRooms() {
  // A room without any device is left out of the house.
  const used = new Set();
  for (const id of devices.keys()) {
    const room = roomOf(id);
    if (room) used.add(room.id);
  }
  return plan.rooms.filter((r) => used.has(r.id));
}

function wallLines(rooms) {
  const lines = new Map();
  const add = (axis, at, a, b) => {
    const key = `${axis}:${at.toFixed(2)}`;
    if (!lines.has(key)) lines.set(key, { axis, at, spans: [] });
    lines.get(key).spans.push([Math.min(a, b), Math.max(a, b)]);
  };
  for (const r of rooms) {
    add("h", r.z, r.x, r.x + r.w);
    add("h", r.z + r.d, r.x, r.x + r.w);
    add("v", r.x, r.z, r.z + r.d);
    add("v", r.x + r.w, r.z, r.z + r.d);
  }
  const out = [];
  for (const line of lines.values()) {
    const cuts = [...new Set(line.spans.flat().map((v) => Math.round(v * 100) / 100))].sort((a, b) => a - b);
    for (let i = 0; i < cuts.length - 1; i++) {
      const [p, q] = [cuts[i], cuts[i + 1]];
      if (q - p < 0.02) continue;
      const mid = (p + q) / 2;
      const count = line.spans.filter(([a, b]) => mid > a && mid < b).length;
      if (!count) continue;
      const last = out[out.length - 1];
      const exterior = count === 1;
      if (last && last.axis === line.axis && last.at === line.at && Math.abs(last.to - p) < 0.02 && last.exterior === exterior) last.to = q;
      else out.push({ axis: line.axis, at: line.at, from: p, to: q, exterior });
    }
  }
  return out;
}

function openingKind(item) {
  const name = `${item.data.id} ${item.data.name || ""}`.toLowerCase();
  if (/window|fenster|ventana/.test(name)) return "window";
  if (/garage/.test(name)) return "garage";
  return "door";
}

function findOpenings(walls) {
  const found = [];
  for (const item of devices.values()) {
    if (item.kind !== "contact") continue;
    const { x, z } = item.group.position;
    let best = null;
    for (const w of walls) {
      const along = w.axis === "h" ? x : z;
      const across = w.axis === "h" ? z : x;
      const dist = Math.abs(across - w.at);
      if (along < w.from - 0.2 || along > w.to + 0.2 || dist > 0.5) continue;
      if (!best || dist < best.dist) best = { wall: w, along, dist };
    }
    if (!best) continue;
    const type = openingKind(item);
    const width = type === "garage" ? 2.6 : type === "window" ? 1.2 : 0.95;
    const center = Math.min(Math.max(best.along, best.wall.from + width / 2 + 0.1), best.wall.to - width / 2 - 0.1);
    found.push({ id: item.data.id, axis: best.wall.axis, at: best.wall.at, center, width, type });
  }
  return found;
}

const wallMaterial = new THREE.MeshStandardMaterial({ color: 0xf7f7f4, roughness: 0.92 });
const capMaterial = new THREE.MeshStandardMaterial({ color: 0x334155, roughness: 0.6 });
const glassMaterial = new THREE.MeshStandardMaterial({ color: 0xbfe0ff, roughness: 0.1, metalness: 0.1, transparent: true, opacity: 0.45 });
const doorMaterial = new THREE.MeshStandardMaterial({ color: 0xe8e3da, roughness: 0.7 });

function wallBox(axis, at, from, to, y0, y1, thick, material) {
  const len = to - from;
  if (len <= 0.02 || y1 <= y0) return null;
  const geo = axis === "h" ? new THREE.BoxGeometry(len, y1 - y0, thick) : new THREE.BoxGeometry(thick, y1 - y0, len);
  const mesh = new THREE.Mesh(geo, material);
  const c = (from + to) / 2;
  mesh.position.set(axis === "h" ? c : at, (y0 + y1) / 2, axis === "h" ? at : c);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  return mesh;
}

function clearArchitecture() {
  architecture.traverse((o) => {
    if (o.isCSS2DObject) o.element.remove();
    if (o.geometry) o.geometry.dispose();
    if (o.material && o.material.map && o.material.map !== POOL && o.material.map !== SOFT_SHADOW) o.material.map.dispose();
  });
  architecture.clear();
}

function buildArchitecture() {
  const rooms = visibleRooms();
  const inside = rooms.filter((r) => !r.outside);
  const walls = wallLines(inside);
  const found = findOpenings(walls);
  const signature = JSON.stringify([rooms.map((r) => r.id), found.map((o) => [o.id, o.axis, o.at, o.center.toFixed(2)])]);
  if (signature === archSignature) return;
  archSignature = signature;
  clearArchitecture();
  openings = found;

  if (inside.length) {
    const xs = inside.flatMap((r) => [r.x, r.x + r.w]), zs = inside.flatMap((r) => [r.z, r.z + r.d]);
    const w = Math.max(...xs) - Math.min(...xs), d = Math.max(...zs) - Math.min(...zs);
    const shadow = new THREE.Mesh(new THREE.PlaneGeometry(w * 1.5, d * 1.6),
      new THREE.MeshBasicMaterial({ map: SOFT_SHADOW, transparent: true, depthWrite: false }));
    shadow.rotation.x = -Math.PI / 2;
    shadow.position.set((Math.min(...xs) + Math.max(...xs)) / 2, 0.005, (Math.min(...zs) + Math.max(...zs)) / 2);
    architecture.add(shadow);
  }

  for (const room of rooms) {
    const h = room.outside ? 0.03 : FLOOR_Y;
    const floor = new THREE.Mesh(new THREE.BoxGeometry(room.w, h, room.d), floorMaterial(room));
    floor.position.set(room.x + room.w / 2, h / 2, room.z + room.d / 2);
    floor.receiveShadow = true;
    architecture.add(floor);
    const label = new CSS2DObject(el("div", { class: room.outside ? "room-tag outside" : "room-tag", text: room.name }));
    label.position.set(room.x + room.w / 2, 0.12, room.outside ? room.z + room.d / 2 : room.z + room.d - 0.55);
    architecture.add(label);
  }

  for (const wall of walls) {
    const thick = wall.exterior ? 0.22 : 0.12;
    let pieces = [[wall.from, wall.to]];
    for (const o of openings.filter((o) => o.axis === wall.axis && Math.abs(o.at - wall.at) < 0.01)) {
      const a = o.center - o.width / 2, b = o.center + o.width / 2;
      if (a < wall.from - 0.01 || b > wall.to + 0.01) continue;
      pieces = pieces.flatMap(([p, q]) => (b <= p || a >= q ? [[p, q]] : [[p, a], [b, q]].filter(([u, v]) => v - u > 0.02)));
      if (o.type === "window") {
        const sill = wallBox(wall.axis, wall.at, a, b, FLOOR_Y, 0.85, thick, wallMaterial);
        if (sill) architecture.add(sill);
      }
    }
    for (const [p, q] of pieces) {
      const body = wallBox(wall.axis, wall.at, p, q, FLOOR_Y, WALL_H, thick, wallMaterial);
      const cap = wallBox(wall.axis, wall.at, p, q, WALL_H, WALL_H + 0.018, thick + 0.004, capMaterial);
      if (body) architecture.add(body);
      if (cap) architecture.add(cap);
    }
  }

  // doors, windows and garage doors that follow their sensors
  for (const o of openings) {
    const hinge = new THREE.Group();
    let leaf;
    if (o.type === "window") {
      leaf = new THREE.Mesh(new THREE.BoxGeometry(o.width - 0.06, WALL_H - 0.87, 0.03), glassMaterial);
      leaf.position.set((o.width - 0.06) / 2, 0.85 + (WALL_H - 0.87) / 2, 0);
    } else {
      leaf = new THREE.Mesh(new THREE.BoxGeometry(o.width - 0.06, WALL_H - FLOOR_Y - 0.02, o.type === "garage" ? 0.06 : 0.045), doorMaterial);
      leaf.position.set((o.width - 0.06) / 2, FLOOR_Y + (WALL_H - FLOOR_Y) / 2, 0);
    }
    leaf.castShadow = true;
    hinge.add(leaf);
    const start = o.center - o.width / 2 + 0.03;
    if (o.axis === "h") hinge.position.set(start, 0, o.at);
    else {
      hinge.position.set(o.at, 0, start);
      hinge.rotation.y = -Math.PI / 2;
    }
    hinge.userData = { base: hinge.rotation.y, leaf, amount: 0 };
    architecture.add(hinge);
    o.hinge = hinge;
  }
}

function animateOpenings(dt) {
  for (const o of openings) {
    if (!o.hinge) continue;
    const item = devices.get(o.id);
    const open = !!(item && item.data.readings && item.data.readings.is_open);
    const u = o.hinge.userData;
    u.amount += ((open ? 1 : 0) - u.amount) * Math.min(1, dt * 4);
    if (o.type === "garage") {
      u.leaf.scale.y = 1 - 0.85 * u.amount;
      u.leaf.position.y = FLOOR_Y + (WALL_H - FLOOR_Y) / 2 + (WALL_H - FLOOR_Y) * 0.42 * u.amount;
    } else {
      o.hinge.rotation.y = u.base + (o.type === "window" ? 0.6 : 1.35) * u.amount;
    }
  }
}

// ---- devices ------------------------------------------------------------------------------------------------------------
function kindOf(d) {
  if (d.kind === "light") return "light";
  if (d.kind === "plug" || d.kind === "switch") return "plug";
  if (d.kind === "camera") return "camera";
  if (d.kind === "doorbell") return "doorbell";
  if (d.kind === "hub") return "hub";
  const r = d.readings || {};
  if ("temperature" in r) return "thermo";
  if ("water_alert" in r || "water_leak" in r) return "water";
  if ("is_open" in r) return "contact";
  return "generic";
}

const PIN_HEIGHT = { light: 2.05, camera: 1.85, doorbell: 1.5, contact: 1.35, thermo: 1.45, hub: 1.1, plug: 0.95, water: 0.75, generic: 1.1 };

function iconFor(item) {
  const d = item.data;
  switch (item.kind) {
    case "light": return "bulb";
    case "plug": return /lamp/i.test(`${d.name || ""} ${d.id}`) ? "lamp" : "plug";
    case "camera": return "camera";
    case "doorbell": return "bell";
    case "hub": return "hub";
    case "thermo": return "thermo";
    case "water": return "drop";
    case "contact": return { window: "window", garage: "garage" }[openingKind(item)] || "door";
    default: return "dot";
  }
}

function fmt(v, digits = 1) {
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(digits);
  return v == null ? "-" : String(v);
}

function look(item) {
  // tone: the colour of the device everywhere it is shown; text: its state in a few words; value: a short reading
  const d = item.data, r = d.readings || {};
  if (!d.online) return { tone: "off", text: "Offline", value: "" };
  switch (item.kind) {
    case "light": {
      const b = (d.settings || {}).brightness;
      return d.on ? { tone: "amber", text: `On, ${b ?? "-"}%`, value: `${b ?? ""}%` } : { tone: "off", text: "Off", value: "Off" };
    }
    case "plug": {
      const w = r.current_consumption;
      return d.on ? { tone: "green", text: w != null ? `On, ${fmt(w)} W` : "On", value: "On" } : { tone: "off", text: "Off", value: "Off" };
    }
    case "contact": {
      const what = { window: "Window", garage: "Garage door" }[openingKind(item)] || "Door";
      return r.is_open ? { tone: "red", text: `${what} open`, value: "Open" } : { tone: "green", text: `${what} closed`, value: "Closed" };
    }
    case "water":
      return r.water_alert || r.water_leak ? { tone: "red", text: "Water detected", value: "Leak" } : { tone: "blue", text: "Dry", value: "Dry" };
    case "thermo":
      return { tone: "blue", text: `${fmt(r.temperature)} °C, ${fmt(r.humidity, 0)}% humidity`, value: `${fmt(r.temperature)}°` };
    case "camera":
    case "doorbell":
      return { tone: "blue", text: d.battery != null ? `Online, battery ${d.battery}%` : "Online", value: d.battery != null ? `${d.battery}%` : "" };
    case "hub":
      return { tone: "green", text: "Online", value: "" };
    default:
      return { tone: "blue", text: "Online", value: "" };
  }
}

let selected = null;
let editing = false;
let dirty = false;
let dragging = null;

function makeDevice(d) {
  const kind = kindOf(d);
  const group = new THREE.Group();
  const parts = {};
  const h = PIN_HEIGHT[kind] || 1.1;

  const puck = new THREE.Mesh(new THREE.CylinderGeometry(0.13, 0.13, 0.02, 24), new THREE.MeshStandardMaterial({ color: 0x64748b, roughness: 0.6 }));
  puck.position.y = FLOOR_Y + 0.012;
  parts.puck = puck;
  const stem = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, FLOOR_Y + 0.02, 0), new THREE.Vector3(0, h - 0.2, 0)]),
    new THREE.LineBasicMaterial({ color: 0x475569, transparent: true, opacity: 0.35 }));
  group.add(puck, stem);

  if (kind === "light") {
    parts.bulb = new THREE.Mesh(new THREE.SphereGeometry(0.11, 24, 16), new THREE.MeshStandardMaterial({ color: 0xdedede, emissive: 0x000000, roughness: 0.3 }));
    parts.bulb.position.y = h - 0.32;
    parts.glow = new THREE.PointLight(0xffffff, 0, 6.5, 1.8);
    parts.glow.position.y = h - 0.4;
    parts.pool = new THREE.Mesh(new THREE.PlaneGeometry(4.2, 4.2),
      new THREE.MeshBasicMaterial({ map: POOL, color: 0xffffff, transparent: true, opacity: 0, depthWrite: false, blending: THREE.AdditiveBlending }));
    parts.pool.rotation.x = -Math.PI / 2;
    parts.pool.position.y = FLOOR_Y + 0.004;
    group.add(parts.bulb, parts.glow, parts.pool);
  } else if (kind === "camera" || kind === "doorbell") {
    const fov = THREE.MathUtils.degToRad(kind === "camera" ? 78 : 140);
    const radius = kind === "camera" ? 5.2 : 2.6;
    const facing = new THREE.Group();
    const shape = new THREE.CircleGeometry(radius, 40, Math.PI / 2 - fov / 2, fov);
    const sector = new THREE.Mesh(shape, new THREE.MeshBasicMaterial({ color: 0x3b82f6, transparent: true, opacity: 0.1, depthWrite: false, side: THREE.DoubleSide }));
    sector.rotation.x = -Math.PI / 2;
    sector.position.y = FLOOR_Y + 0.006;
    const edge = new THREE.LineSegments(new THREE.EdgesGeometry(shape), new THREE.LineBasicMaterial({ color: 0x3b82f6, transparent: true, opacity: 0.35 }));
    edge.rotation.x = -Math.PI / 2;
    edge.position.y = FLOOR_Y + 0.008;
    facing.add(sector, edge);
    parts.facing = facing;
    parts.sector = sector;
    group.add(facing);
  }

  const pin = el("div", { class: "pin" });
  const badge = el("div", { class: "pin-badge" });
  const label = el("div", { class: "pin-label" }, el("span", { class: "pin-name" }), el("span", { class: "value" }));
  pin.append(badge, label);
  const pinObject = new CSS2DObject(el("div", {}, pin));
  pinObject.position.y = h;
  group.add(pinObject);
  scene.add(group);

  const item = { data: d, kind, group, parts, pin, badge, label, iconName: "" };
  pin.addEventListener("pointerdown", (ev) => startDrag(ev, item));
  pin.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (item.justDragged) {
      item.justDragged = false;
      return;
    }
    select(item.data.id, true);
  });
  return item;
}

function kelvinToColor(k) {
  const t = k / 100;
  let r, g, b;
  if (t <= 66) { r = 255; g = 99.47 * Math.log(t) - 161.12; b = t <= 19 ? 0 : 138.52 * Math.log(t - 10) - 305.04; }
  else { r = 329.7 * (t - 60) ** -0.1332; g = 288.12 * (t - 60) ** -0.0755; b = 255; }
  const c = (v) => Math.min(255, Math.max(0, v)) / 255;
  return new THREE.Color().setRGB(c(r), c(g), c(b), THREE.SRGBColorSpace);
}

function hsvToHex(h, s, v = 100) {
  const f = (n) => {
    const k = (n + h / 60) % 6;
    return v / 100 - (v / 100) * (s / 100) * Math.max(0, Math.min(k, 4 - k, 1));
  };
  return "#" + [f(5), f(3), f(1)].map((x) => Math.round(x * 255).toString(16).padStart(2, "0")).join("");
}

function hexToHueSat(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const max = Math.max(r, g, b), min = Math.min(r, g, b), span = max - min;
  let h = 0;
  if (span) {
    if (max === r) h = ((g - b) / span) % 6;
    else if (max === g) h = (b - r) / span + 2;
    else h = (r - g) / span + 4;
  }
  return { hue: Math.round((h * 60 + 360) % 360), saturation: Math.round(max === 0 ? 0 : (span / max) * 100) };
}

function lightColor(settings) {
  const temp = Number(settings.color_temperature || 0);
  if (temp > 0) return kelvinToColor(temp);
  const hsv = Array.isArray(settings.hsv) ? settings.hsv : [0, 0, 100];
  return new THREE.Color(hsvToHex(hsv[0] % 360, Math.min(100, hsv[1] || 0), 100));
}

const TONE_COLOR = { amber: 0xf59e0b, green: 0x16a34a, red: 0xdc2626, blue: 0x0284c7, off: 0x94a3b8 };

function paint(item) {
  const d = item.data;
  const state = look(item);
  item.state = state;
  const name = iconFor(item);
  if (name !== item.iconName) {
    item.badge.replaceChildren(icon(name));
    item.iconName = name;
  }
  item.pin.className = `pin tone-${state.tone}` + (d.online ? "" : " offline") + (d.id === selected ? " selected" : "")
    + (state.tone === "red" || item.kind === "thermo" ? " keep" : "");
  item.label.firstChild.textContent = d.name || d.id;
  item.label.lastChild.textContent = state.value;
  item.parts.puck.material.color.setHex(TONE_COLOR[state.tone] || 0x64748b);
  if (item.kind === "light") {
    const on = d.online && d.on;
    const color = lightColor(d.settings || {});
    const level = on ? Math.max(0.12, ((d.settings || {}).brightness || 100) / 100) : 0;
    item.parts.bulb.material.color.copy(on ? color : new THREE.Color(0xdedede));
    item.parts.bulb.material.emissive.copy(on ? color : new THREE.Color(0x000000));
    item.parts.bulb.material.emissiveIntensity = on ? 1.2 : 0;
    item.parts.glow.color.copy(color);
    item.parts.glow.intensity = on ? 7 * level : 0;
    item.parts.pool.material.color.copy(color);
    item.parts.pool.material.opacity = on ? 0.55 * level : 0;
  }
  if (item.parts.facing) {
    item.parts.facing.rotation.y = THREE.MathUtils.degToRad(Number((plan.devices[d.id] || {}).yaw || 0));
    item.parts.sector.material.opacity = d.online ? 0.1 : 0.04;
  }
}

function placeFor(id, index) {
  const spot = plan.devices[id];
  if (spot) return spot;
  const rooms = plan.rooms.length ? plan.rooms : [{ x: -8, z: -6, w: 16, d: 12 }];
  const minX = Math.min(...rooms.map((r) => r.x));
  const maxZ = Math.max(...rooms.map((r) => r.z + r.d));
  return { x: minX + 1 + (index % 10) * 1.6, z: maxZ + 2.2 + Math.floor(index / 10) * 1.6 };
}

function forget(item) {
  item.group.traverse((o) => { if (o.isCSS2DObject) o.element.remove(); });
  scene.remove(item.group);
}

function syncDevices(list, complete) {
  const seen = new Set();
  let unplaced = 0;
  for (const d of list) {
    seen.add(d.id);
    let item = devices.get(d.id);
    if (item && item.kind !== kindOf(d)) {
      forget(item);
      devices.delete(d.id);
      item = null;
    }
    if (!item) {
      item = makeDevice(d);
      devices.set(d.id, item);
    }
    item.data = d;
    const spot = placeFor(d.id, plan.devices[d.id] ? 0 : unplaced++);
    if (!dragging || dragging.item !== item) item.group.position.set(spot.x, spot.y || 0, spot.z);
  }
  if (complete) {
    for (const [id, item] of devices) {
      if (!seen.has(id)) {
        forget(item);
        devices.delete(id);
      }
    }
  }
  buildArchitecture();
  for (const item of devices.values()) paint(item);
}

// ---- summary, sidebar ---------------------------------------------------------------------------------------------------
function renderSummary() {
  const box = document.getElementById("summary");
  box.replaceChildren();
  const all = [...devices.values()];
  const lights = all.filter((i) => i.kind === "light");
  const on = lights.filter((i) => i.data.online && i.data.on).length;
  const contacts = all.filter((i) => i.kind === "contact");
  const open = contacts.filter((i) => (i.data.readings || {}).is_open).length;
  const wet = all.filter((i) => i.kind === "water" && ((i.data.readings || {}).water_alert || (i.data.readings || {}).water_leak)).length;
  const offline = all.filter((i) => !i.data.online).length;
  const thermo = all.find((i) => i.kind === "thermo" && i.data.online);
  const chip = (tone, iconName, text) => el("span", { class: `chip ${tone}` }, icon(iconName), el("span", { text }));
  if (lights.length) box.append(chip(on ? "amber" : "", "bulb", `${on} of ${lights.length} lights on`));
  if (contacts.length) box.append(open ? chip("red", "alert", `${open} open`) : chip("green", "check", "All doors and windows closed"));
  if (all.some((i) => i.kind === "water")) box.append(wet ? chip("red", "drop", "Water detected") : chip("blue", "drop", "No leaks"));
  if (thermo) box.append(chip("", "thermo", `${fmt((thermo.data.readings || {}).temperature)} °C`));
  if (offline) box.append(chip("red", "offline", `${offline} offline`));
}

function renderSidebar() {
  const list = document.getElementById("room-list");
  list.replaceChildren();
  const groups = new Map();
  for (const room of visibleRooms()) groups.set(room.id, { room, items: [] });
  const loose = { room: { name: T.notPlaced }, items: [] };
  for (const item of devices.values()) {
    const room = roomOf(item.data.id);
    (room && groups.get(room.id) ? groups.get(room.id) : loose).items.push(item);
  }
  for (const { room, items } of [...groups.values(), loose]) {
    if (!items.length) continue;
    const section = el("div", { class: "room" },
      el("div", { class: "room-title" }, document.createTextNode(room.name), el("span", { text: String(items.length) })));
    items.sort((a, b) => (a.data.name || a.data.id).localeCompare(b.data.name || b.data.id));
    for (const item of items) {
      const state = item.state || look(item);
      section.append(el("button", {
        class: `device-row tone-${state.tone}` + (selected === item.data.id ? " selected" : ""), type: "button",
        on: { click: () => select(item.data.id, true) },
      }, el("span", { class: "icon" }, icon(iconFor(item))),
         el("span", { class: "text" }, el("span", { class: "name", text: item.data.name || item.data.id }), el("span", { class: "state", text: state.text }))));
    }
    list.append(section);
  }
}

// ---- the inspector ------------------------------------------------------------------------------------------------------
const inspector = document.getElementById("inspector");
const detailCache = {};

function select(id, fly = false) {
  const before = selected;
  if (before !== id) stopLive();
  selected = id;
  for (const key of [before, id]) {
    const item = key && devices.get(key);
    if (item) paint(item);
  }
  renderInspector(true);
  renderSidebar();
  fitView();
  const item = devices.get(id);
  if (fly && item) flyTo(item.group.position);
}

function closeInspector() {
  const before = selected;
  selected = null;
  stopLive();
  inspector.hidden = true;
  const item = before && devices.get(before);
  if (item) paint(item);
  renderSidebar();
  fitView();
}

async function command(path, body) {
  const err = inspector.querySelector(".error-text");
  if (err) err.textContent = "";
  try {
    const updated = await api(path, { method: "POST", body });
    const item = devices.get(updated.id);
    if (item) {
      item.data = { ...item.data, ...updated };
      paint(item);
      renderSummary();
      renderSidebar();
      renderInspector(false);
    }
  } catch (e) {
    const box = inspector.querySelector(".error-text");
    if (box) box.textContent = e.message;
    else toast(e.message, true);
  }
}

function featureOf(id, feature) {
  return ((detailCache[id] || {}).features || []).find((f) => f.id === feature);
}

const SWATCHES = [[0, 100], [28, 100], [52, 100], [120, 85], [185, 90], [222, 95], [272, 80], [320, 70]];

function renderInspector(rebuild) {
  const item = devices.get(selected);
  if (!item) {
    inspector.hidden = true;
    return;
  }
  const d = item.data;
  const state = item.state || look(item);
  if (!rebuild && inspector.dataset.device === d.id) {
    // Refresh what the owner is not holding: the state, the switch and the data. Sliders and video keep going.
    const sub = inspector.querySelector(".insp-title p");
    if (sub) sub.textContent = subtitle(item);
    const sw = inspector.querySelector(".switch");
    if (sw) sw.setAttribute("aria-checked", String(!!d.on));
    const swText = inspector.querySelector(".switch-row .muted");
    if (swText) swText.textContent = state.text;
    const big = inspector.querySelector(".hero .big");
    if (big) {
      const hero = heroFor(item);
      big.textContent = hero.big;
      big.className = hero.red ? "big red" : "big";
      inspector.querySelector(".hero .sub").textContent = hero.sub;
    }
    const iconBox = inspector.querySelector(".insp-icon");
    if (iconBox) iconBox.className = `insp-icon tone-${state.tone}`;
    fillData(inspector.querySelector(".data"), d);
    const pills = inspector.querySelector(".pills");
    if (pills) pills.replaceWith(pillsFor(d));
    return;
  }
  inspector.dataset.device = d.id;
  inspector.hidden = false;
  inspector.replaceChildren();
  const caps = new Set(d.capabilities || []);
  const head = el("div", { class: "insp-head" },
    el("div", { class: `insp-icon tone-${state.tone}` }, icon(iconFor(item))),
    el("div", { class: "insp-title" }, el("h2", { text: d.name || d.id }), el("p", { text: subtitle(item) })),
    el("button", { class: "icon-btn", type: "button", "aria-label": "Close", on: { click: closeInspector } }, icon("close")));
  const body = el("div", { class: "insp-body" });
  inspector.append(head, body);
  body.append(pillsFor(d));

  if (caps.has("snapshot")) body.append(cameraBlock(d));

  if (["thermo", "contact", "water"].includes(item.kind)) {
    const hero = heroFor(item);
    body.append(el("div", { class: "hero" }, el("span", { class: hero.red ? "big red" : "big", text: hero.big }), el("span", { class: "sub", text: hero.sub })));
  }

  if (caps.has("power")) {
    const sw = el("button", { class: item.kind === "light" ? "switch amber" : "switch", type: "button", role: "switch",
      "aria-checked": String(!!d.on), "aria-label": "Power", disabled: !d.online,
      on: { click: () => command(`/devices/${encodeURIComponent(d.id)}/power`, { on: !item.data.on }) } });
    body.append(el("div", { class: "switch-row" }, el("div", {}, el("strong", { text: "Power" }), el("div", { class: "muted", text: state.text })), sw));
  }

  if (caps.has("brightness")) {
    const f = featureOf(d.id, "brightness") || {};
    const current = (d.settings || {}).brightness ?? 50;
    const value = el("span", { class: "value", text: `${current}%` });
    const range = el("input", { type: "range", class: "range-brightness", min: f.min ?? 1, max: f.max ?? 100, value: current, "aria-label": "Brightness" });
    range.addEventListener("input", () => { value.textContent = `${range.value}%`; });
    range.addEventListener("change", () => command(`/devices/${encodeURIComponent(d.id)}/light`, { brightness: Number(range.value) }));
    body.append(el("div", { class: "control" }, el("div", { class: "control-head" }, el("span", { text: "Brightness" }), value), range));
  }

  if (caps.has("color_temp")) {
    const f = featureOf(d.id, "color_temperature") || {};
    const current = Number((d.settings || {}).color_temperature) || 2700;
    const value = el("span", { class: "value", text: `${current} K` });
    const range = el("input", { type: "range", class: "range-kelvin", min: f.min || 2500, max: f.max || 6500, step: 100, value: current, "aria-label": "White temperature" });
    range.addEventListener("input", () => { value.textContent = `${range.value} K`; });
    range.addEventListener("change", () => command(`/devices/${encodeURIComponent(d.id)}/light`, { color_temp: Number(range.value) }));
    body.append(el("div", { class: "control" }, el("div", { class: "control-head" }, el("span", { text: "White" }), value), range));
  }

  if (caps.has("color")) {
    const hsv = Array.isArray((d.settings || {}).hsv) ? d.settings.hsv : [0, 0, 100];
    const inColor = !Number((d.settings || {}).color_temperature);
    const row = el("div", { class: "swatches" });
    for (const [hue, sat] of SWATCHES) {
      const hex = hsvToHex(hue, sat, 100);
      const active = inColor && Math.abs(hsv[0] - hue) < 6 && Math.abs(hsv[1] - sat) < 8;
      const sw = el("button", { class: active ? "swatch active" : "swatch", type: "button", "aria-label": `Color ${hex}`,
        on: { click: () => command(`/devices/${encodeURIComponent(d.id)}/light`, { hue, saturation: sat }) } });
      sw.style.background = hex;
      row.append(sw);
    }
    const picker = el("input", { type: "color", value: hsvToHex(hsv[0] % 360, hsv[1], 100), "aria-label": "Custom color" });
    picker.addEventListener("change", () => command(`/devices/${encodeURIComponent(d.id)}/light`, hexToHueSat(picker.value)));
    row.append(picker);
    body.append(el("div", { class: "control" }, el("div", { class: "control-head" }, el("span", { text: "Color" })), row));
  }

  if ((item.kind === "camera" || item.kind === "doorbell") && editing) {
    const spot = plan.devices[d.id] || {};
    const value = el("span", { class: "value", text: `${Math.round(spot.yaw || 0)}°` });
    const range = el("input", { type: "range", min: -180, max: 180, step: 5, value: spot.yaw || 0, "aria-label": "Facing" });
    range.addEventListener("input", () => {
      value.textContent = `${range.value}°`;
      const p = item.group.position;
      plan.devices[d.id] = { ...(plan.devices[d.id] || { x: p.x, z: p.z }), yaw: Number(range.value) };
      paint(item);
      markDirty();
    });
    body.append(el("div", { class: "control" }, el("div", { class: "control-head" }, el("span", { text: "Facing" }), value), range));
  }

  body.append(el("div", { class: "error-text" }));
  body.append(el("div", { class: "section-title", text: "Details" }));
  const table = el("div", { class: "data" });
  fillData(table, d);
  body.append(table);

  const more = el("details", { class: "more" }, el("summary", { text: "All data" }));
  const all = el("div", { class: "data" });
  more.append(all);
  more.addEventListener("toggle", async () => {
    if (!more.open) return;
    try {
      const detail = await api(`/devices/${encodeURIComponent(d.id)}`);
      detailCache[d.id] = detail;
      all.replaceChildren();
      for (const f of detail.features || []) {
        if (f.type === "Action") continue;
        const v = f.value == null ? "-" : typeof f.value === "object" ? JSON.stringify(f.value) : String(f.value);
        all.append(el("div", { text: f.name || f.id }), el("div", { text: f.unit ? `${v} ${f.unit}` : v }));
      }
    } catch (e) {
      all.replaceChildren(el("div", { text: e.message }), el("div"));
    }
  });
  body.append(more);

  if (!detailCache[d.id] && (caps.has("brightness") || caps.has("color_temp"))) {
    api(`/devices/${encodeURIComponent(d.id)}`).then((detail) => {
      detailCache[d.id] = detail;
      if (selected === d.id) renderInspector(true);
    }).catch(() => {});
  }
}

function heroFor(item) {
  const r = item.data.readings || {};
  if (item.kind === "thermo") return { big: `${fmt(r.temperature)} °C`, sub: `${fmt(r.humidity, 0)}% humidity` };
  if (item.kind === "water") {
    const wet = r.water_alert || r.water_leak;
    return { big: wet ? "Leak" : "Dry", sub: wet ? "Water detected" : "No water detected", red: !!wet };
  }
  const what = { window: "Window", garage: "Garage door" }[openingKind(item)] || "Door";
  return { big: r.is_open ? "Open" : "Closed", sub: what, red: !!r.is_open };
}

function subtitle(item) {
  const d = item.data;
  const room = roomOf(d.id);
  return [T.kinds[d.kind] || d.kind, d.model, room ? room.name : T.notPlaced].filter(Boolean).join(" · ");
}

function pillsFor(d) {
  const box = el("div", { class: "pills" });
  box.append(el("span", { class: d.online ? "pill green" : "pill red" }, el("span", { class: "dot" }), el("span", { text: d.online ? "Online" : "Offline" })));
  if (d.battery != null) box.append(el("span", { class: d.battery < 20 ? "pill red" : "pill", text: `Battery ${d.battery}%` }));
  if (d.parent) box.append(el("span", { class: "pill", text: `Via ${d.parent}` }));
  return box;
}

function fillData(table, d) {
  if (!table) return;
  table.replaceChildren();
  const rows = [];
  for (const [k, v] of Object.entries(d.readings || {})) {
    const unit = (d.units || {})[k];
    const shown = typeof v === "boolean" ? (v ? "Yes" : "No") : fmt(v);
    rows.push([T.readings[k] || k, unit === "celsius" ? `${shown} °C` : unit ? `${shown} ${unit}` : shown]);
  }
  const s = d.settings || {};
  if (s.brightness != null) rows.push(["Brightness", `${s.brightness}%`]);
  if (s.color_temperature) rows.push(["White", `${s.color_temperature} K`]);
  rows.push(["Model", d.model]);
  if (d.host) rows.push(["Address", d.host]);
  for (const [k, v] of rows) table.append(el("div", { text: k }), el("div", { text: String(v) }));
}

// ---- live video -----------------------------------------------------------------------------------------------------------
// GET /devices/{id}/live sends JPEG pictures one after the other (multipart/x-mixed-replace). They are read with fetch,
// so the access token goes along, and drawn on a canvas. Closing the inspector, picking another device or hiding the
// page stops the stream, and the server then closes the camera's video.
let live = null;
const CRLF2 = [13, 10, 13, 10];

function findSequence(buf, seq) {
  outer: for (let i = 0; i + seq.length <= buf.length; i++) {
    for (let j = 0; j < seq.length; j++) if (buf[i + j] !== seq[j]) continue outer;
    return i;
  }
  return -1;
}

function stopLive() {
  if (!live) return;
  live.controller.abort();
  live.ui.badge.hidden = true;
  live = null;
}

async function drawFrame(session, bytes) {
  const bitmap = await createImageBitmap(new Blob([bytes], { type: "image/jpeg" }));
  for (const canvas of session.canvases) {
    if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
    }
    canvas.getContext("2d").drawImage(bitmap, 0, 0);
  }
  bitmap.close();
  if (!session.frames++) {
    session.ui.wait.hidden = true;
    session.ui.badge.hidden = false;
  }
}

async function startLive(d, ui, high, retry = false) {
  stopLive();
  const session = { id: d.id, controller: new AbortController(), canvases: new Set([ui.canvas]), ui, frames: 0 };
  if (!viewer.hidden && viewerFor === d.id) session.canvases.add(viewerCanvas);
  live = session;
  ui.wait.textContent = "Connecting to the camera";
  ui.wait.hidden = false;
  ui.badge.hidden = true;
  ui.play.replaceChildren(icon("pause"), document.createTextNode("Pause"));
  try {
    const res = await fetch(`/devices/${encodeURIComponent(d.id)}/live?quality=${high ? "high" : "low"}`,
      { headers: authHeaders(), signal: session.controller.signal });
    if (res.status === 409) throw new Error("This camera is already showing live video in another window.");
    if (!res.ok) throw await failure(res);
    const reader = res.body.getReader();
    let buf = new Uint8Array(0);
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      const joined = new Uint8Array(buf.length + value.length);
      joined.set(buf);
      joined.set(value, buf.length);
      buf = joined;
      for (;;) {
        const end = findSequence(buf, CRLF2);
        if (end < 0) break;
        const head = new TextDecoder().decode(buf.subarray(0, end));
        const m = /content-length:\s*(\d+)/i.exec(head);
        if (!m) {
          buf = buf.subarray(end + 4);
          continue;
        }
        const start = end + 4, length = Number(m[1]);
        if (buf.length < start + length) break;
        const picture = buf.slice(start, start + length);
        buf = buf.slice(start + length);
        if (live !== session) return;
        await drawFrame(session, picture);
      }
    }
    if (live === session) {
      ui.wait.textContent = "The live view ended. Press Play to watch again.";
      ui.wait.hidden = false;
      ui.badge.hidden = true;
      ui.play.replaceChildren(icon("play"), document.createTextNode("Play"));
      live = null;
    }
  } catch (e) {
    if (e.name === "AbortError") return;
    if (live === session) {
      // A TypeError is the browser losing the connection (the server restarted, the network dropped): try once more.
      const lost = e instanceof TypeError;
      ui.wait.textContent = lost ? (retry ? "The connection to tolpo was lost. Press Play to try again." : "The connection was lost. Trying again") : e.message;
      ui.wait.hidden = false;
      ui.badge.hidden = true;
      ui.play.replaceChildren(icon("play"), document.createTextNode("Play"));
      live = null;
      if (lost && !retry) setTimeout(() => { if (selected === d.id && !live) startLive(d, ui, high, true); }, 2000);
    }
  }
}

function cameraBlock(d) {
  const canvas = el("canvas", { class: "cam-canvas", "aria-label": `Live video from ${d.name || d.id}` });
  const wait = el("span", { class: "cam-wait", text: "Connecting to the camera" });
  const badge = el("span", { class: "live-badge" }, el("span", { class: "live-dot" }), document.createTextNode("LIVE"));
  badge.hidden = true;
  const frame = el("div", { class: "cam-frame" }, canvas, wait, badge);
  const hd = el("input", { type: "checkbox" });
  const play = el("button", { class: "btn small primary", type: "button" });
  const ui = { canvas, wait, badge, play };
  play.addEventListener("click", () => {
    if (live && live.id === d.id) {
      stopLive();
      wait.textContent = "Paused";
      wait.hidden = false;
      play.replaceChildren(icon("play"), document.createTextNode("Play"));
    } else {
      startLive(d, ui, hd.checked);
    }
  });
  hd.addEventListener("change", () => startLive(d, ui, hd.checked));
  const block = el("div", { class: "control" },
    frame,
    el("div", { class: "cam-tools" },
      play,
      el("label", { class: "check" }, hd, document.createTextNode("HD")),
      el("span", { class: "grow" }),
      el("button", { class: "btn small ghost", type: "button", on: { click: () => openViewer(d) } }, icon("expand"), document.createTextNode("Expand"))));
  if (d.battery != null) block.append(el("div", { class: "note", text: T.batteryNote }));
  // Clicking a camera shows its live video straight away.
  queueMicrotask(() => startLive(d, ui, false));
  return block;
}

const viewer = document.getElementById("viewer");
const viewerCanvas = document.getElementById("viewer-canvas");
let viewerFor = null;

function openViewer(d) {
  document.getElementById("viewer-title").textContent = d.name || d.id;
  document.getElementById("viewer-meta").textContent = [d.model, roomOf(d.id)?.name].filter(Boolean).join(" · ");
  viewerFor = d.id;
  viewer.hidden = false;
  if (live && live.id === d.id) {
    live.canvases.add(viewerCanvas);
    const small = [...live.canvases].find((c) => c !== viewerCanvas);
    if (small && small.width) {
      viewerCanvas.width = small.width;
      viewerCanvas.height = small.height;
      viewerCanvas.getContext("2d").drawImage(small, 0, 0);
    }
  }
}

function closeViewer() {
  viewer.hidden = true;
  viewerFor = null;
  if (live) live.canvases.delete(viewerCanvas);
}
document.getElementById("viewer-close").addEventListener("click", closeViewer);
viewer.addEventListener("click", (ev) => { if (ev.target === viewer) closeViewer(); });
window.addEventListener("keydown", (ev) => {
  if (ev.key !== "Escape") return;
  if (!viewer.hidden) closeViewer();
  else if (selected) closeInspector();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden && live) {
    const { ui } = live;
    stopLive();
    ui.wait.textContent = "Paused while the page was hidden";
    ui.wait.hidden = false;
    ui.play.replaceChildren(icon("play"), document.createTextNode("Play"));
  }
});

// ---- editing the layout -------------------------------------------------------------------------------------------------
const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
const groundPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);

function groundPoint(ev) {
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.set(((ev.clientX - rect.left) / rect.width) * 2 - 1, -((ev.clientY - rect.top) / rect.height) * 2 + 1);
  raycaster.setFromCamera(pointer, camera);
  const at = new THREE.Vector3();
  return raycaster.ray.intersectPlane(groundPlane, at) ? at : null;
}

function startDrag(ev, item) {
  if (!editing || ev.button !== 0) return;
  ev.preventDefault();
  dragging = { item, moved: false };
  controls.enabled = false;
  item.pin.setPointerCapture(ev.pointerId);
}

window.addEventListener("pointermove", (ev) => {
  if (!dragging) return;
  const at = groundPoint(ev);
  if (!at) return;
  dragging.moved = true;
  dragging.item.group.position.x = Math.round(at.x * 20) / 20;
  dragging.item.group.position.z = Math.round(at.z * 20) / 20;
});

window.addEventListener("pointerup", () => {
  if (!dragging) return;
  const { item, moved } = dragging;
  dragging = null;
  controls.enabled = true;
  if (!moved) return;
  item.justDragged = true;
  const p = item.group.position;
  const room = roomAt(p.x, p.z);
  const spot = { ...(plan.devices[item.data.id] || {}), x: p.x, z: p.z };
  if (room) spot.room = room.id;
  else delete spot.room;
  plan.devices[item.data.id] = spot;
  markDirty();
  buildArchitecture();
  renderSidebar();
  if (selected === item.data.id) renderInspector(true);
});

function markDirty() {
  dirty = true;
  document.getElementById("save").hidden = false;
}

document.getElementById("edit").addEventListener("click", (ev) => {
  editing = !editing;
  ev.currentTarget.setAttribute("aria-pressed", String(editing));
  document.body.classList.toggle("editing", editing);
  if (editing) toast(T.editHint);
  if (selected && !(live && live.id === selected)) renderInspector(true);
});

document.getElementById("save").addEventListener("click", async () => {
  try {
    plan = await api("/home/layout", { method: "PUT", body: plan });
    dirty = false;
    document.getElementById("save").hidden = true;
    toast(T.saved);
  } catch (e) {
    toast(e.message, true);
  }
});

document.getElementById("labels-toggle").addEventListener("click", (ev) => {
  const on = ev.currentTarget.getAttribute("aria-pressed") !== "true";
  ev.currentTarget.setAttribute("aria-pressed", String(on));
  labelRenderer.domElement.classList.toggle("all", on);
});

// ---- the view -----------------------------------------------------------------------------------------------------------
let flight = null;

function flyTo(p) {
  const offset = camera.position.clone().sub(controls.target);
  const want = new THREE.Vector3(p.x, 0.6, p.z);
  offset.setLength(THREE.MathUtils.clamp(offset.length(), 12, 26));
  flight = { from: controls.target.clone(), to: want, camFrom: camera.position.clone(), camTo: want.clone().add(offset), t: 0 };
}

function fitHouse() {
  const rooms = visibleRooms();
  if (!rooms.length) return;
  const xs = rooms.flatMap((r) => [r.x, r.x + r.w]), zs = rooms.flatMap((r) => [r.z, r.z + r.d]);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cz = (Math.min(...zs) + Math.max(...zs)) / 2;
  const size = Math.max(Math.max(...xs) - Math.min(...xs), (Math.max(...zs) - Math.min(...zs)) * 1.3);
  // Fit the tighter side: on a tall phone screen that is the width, not the height.
  const vertical = THREE.MathUtils.degToRad(camera.fov);
  const horizontal = 2 * Math.atan(Math.tan(vertical / 2) * camera.aspect);
  const dist = (size / (2 * Math.tan(Math.min(vertical, horizontal) / 2))) * 1.05;
  controls.target.set(cx, 0, cz);
  camera.position.set(cx + dist * 0.18, dist * 0.78, cz + dist * 0.62);
}

function fitView() {
  const w = window.innerWidth, h = window.innerHeight;
  camera.aspect = w / h;
  const wide = w > 900;
  const left = wide ? 288 : 0;
  const right = wide && !inspector.hidden ? 368 : 0;
  // Centre the scene in what stays visible: below the top bar and, on a narrow screen, above the inspector, which is
  // then a sheet over the bottom.
  const top = document.querySelector(".topbar").getBoundingClientRect().bottom;
  const below = !wide && !inspector.hidden ? h - inspector.getBoundingClientRect().top : 0;
  camera.setViewOffset(w, h, (right - left) / 2, (below - top) / 2, w, h);
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
  labelRenderer.setSize(w, h);
}
window.addEventListener("resize", fitView);

// ---- refresh --------------------------------------------------------------------------------------------------------------
// Every few seconds the server is asked about everything except cameras. Cameras are asked every five minutes only,
// because a question can wake a battery camera and cost it battery.
const QUICK_KINDS = ["light", "plug", "switch", "sensor", "hub", "doorbell"];
let lastFull = 0;

function setStatus(text, error = false) {
  const s = document.getElementById("status");
  s.textContent = text;
  s.classList.toggle("error", error);
}

async function refresh() {
  try {
    const full = Date.now() - lastFull > 300000;
    let list;
    if (full) {
      list = await api("/devices");
      lastFull = Date.now();
    } else {
      list = (await Promise.all(QUICK_KINDS.map((k) => api(`/devices?kind=${k}`).catch(() => [])))).flat();
    }
    syncDevices(list, full);
    renderSummary();
    renderSidebar();
    if (selected) renderInspector(false);
    setStatus(T.updated(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })));
  } catch (e) {
    setStatus(e.message, true);
  }
}

let started = false;
async function start() {
  try {
    plan = await api("/home/layout");
  } catch (e) {
    setStatus(e.message, true);
    return;
  }
  await refresh();
  fitHouse();
  fitView();
  if (!started) {
    started = true;
    setInterval(refresh, 6000);
  }
}

const clock = new THREE.Clock();
renderer.setAnimationLoop(() => {
  const dt = Math.min(clock.getDelta(), 0.1);
  if (flight) {
    flight.t = Math.min(1, flight.t + dt / 0.7);
    const k = 1 - (1 - flight.t) ** 3;
    controls.target.lerpVectors(flight.from, flight.to, k);
    camera.position.lerpVectors(flight.camFrom, flight.camTo, k);
    if (flight.t >= 1) flight = null;
  }
  animateOpenings(dt);
  controls.update();
  renderer.render(scene, camera);
  labelRenderer.render(scene, camera);
});

fitView();
start();
