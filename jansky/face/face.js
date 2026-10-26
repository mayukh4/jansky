"use strict";
// Jansky's face: two vacuum-tube eyes on a night sky, and an oscilloscope trace for a mouth.
// State arrives from Jansky over /events (Server-Sent Events). Open with ?demo to run a scripted loop.

const W = 800, H = 480;
const stage = document.getElementById("stage");
const canvas = document.getElementById("sky");
const ctx = canvas.getContext("2d");
const cardEl = document.getElementById("card");
const timersEl = document.getElementById("timers");
const captionEl = document.getElementById("caption");
const todoEl = document.getElementById("todo");
const todoItemsEl = document.getElementById("todo-items");
const todoMoreEl = document.getElementById("todo-more");
const badgeEl = document.getElementById("todo-badge");
const mutedEl = document.getElementById("muted");
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

function fit() {
  const s = Math.min(innerWidth / W, innerHeight / H);
  stage.style.transform = `scale(${s})`;
}
addEventListener("resize", fit);
fit();

const dpr = Math.min(devicePixelRatio || 1, 2);
canvas.width = W * dpr;
canvas.height = H * dpr;
canvas.style.width = W + "px";
canvas.style.height = H + "px";
ctx.scale(dpr, dpr);

// ---------------------------------------------------------------- springs

class Spring {
  constructor(v, k = 160, d = 19) { this.v = v; this.t = v; this.vel = 0; this.k = k; this.d = d; }
  step(dt) {
    this.vel += (this.k * (this.t - this.v) - this.d * this.vel) * dt;
    this.v += this.vel * dt;
    return this.v;
  }
}

const EXPR = {
  neutral:   { w: 118, h: 150, r: 38, lidTop: 0,    lidTilt: 0,     lidBottom: 0,    glow: 0.85 },
  listening: { w: 128, h: 166, r: 42, lidTop: 0,    lidTilt: 0,     lidBottom: 0,    glow: 1.0 },
  thinking:  { w: 116, h: 122, r: 34, lidTop: 0.16, lidTilt: 0.06,  lidBottom: 0,    glow: 0.8 },
  speaking:  { w: 122, h: 150, r: 40, lidTop: 0,    lidTilt: 0,     lidBottom: 0,    glow: 0.95 },
  happy:     { w: 130, h: 132, r: 58, lidTop: 0,    lidTilt: 0,     lidBottom: 0.72, glow: 1.0 },
  sad:       { w: 112, h: 132, r: 36, lidTop: 0.3,  lidTilt: -0.34, lidBottom: 0,    glow: 0.6 },
  surprised: { w: 110, h: 180, r: 56, lidTop: 0,    lidTilt: 0,     lidBottom: 0,    glow: 1.08 },
  sleepy:    { w: 122, h: 150, r: 38, lidTop: 0.6,  lidTilt: 0,     lidBottom: 0,    glow: 0.5 },
  asleep:    { w: 126, h: 150, r: 38, lidTop: 0.93, lidTilt: 0,     lidBottom: 0,    glow: 0.32 },
};

const S = {};
for (const [k, v] of Object.entries(EXPR.neutral)) S[k] = new Spring(v, 140, 17);
S.gx = new Spring(0, 120, 15);      // gaze, px
S.gy = new Spring(0, 120, 15);
S.faceX = new Spring(0, 130, 22);   // the whole face steps aside for a card
S.scale = new Spring(1, 200, 16);
S.trace = new Spring(1, 260, 22);   // mouth trace amplitude, px
S.rings = new Spring(0, 60, 12);    // listening rings strength

function setExpr(name) {
  for (const [k, v] of Object.entries(EXPR[name])) S[k].t = v;
}

// ---------------------------------------------------------------- what Jansky is doing

const J = {
  mode: "idle",
  emotion: null, emotionUntil: 0,
  spk: 0, mic: 0,                 // normalised output / mic level, 0..1
  doaFront: 0, doaSign: 1,        // DoA calibration (config.DOA_FRONT_DEG / DOA_SIGN)
  lookUntil: 0,                   // how long to keep looking at whoever spoke
  lastActive: performance.now(),
  card: null, cardHideAt: 0,
  timers: [],
  todo: { items: [], shown: false },
  muted: false,
  captions: true,
};

function emote(name, seconds = 2.4) {
  J.emotion = name;
  J.emotionUntil = performance.now() + seconds * 1000;
}

const OPENER_MOOD = { "ha": "happy", "brilliant": "happy", "hello": "happy", "hi": "happy", "ooh": "surprised",
                      "oh dear": "sad", "hmm": null };

function handle(ev) {
  const now = performance.now();
  switch (ev.type) {
    case "hello":
      J.doaFront = ev.doa_front ?? 0;
      J.doaSign = ev.doa_sign ?? 1;
      J.captions = ev.captions ?? true;
      break;
    case "state":
      J.mode = ev.state;
      J.lastActive = now;
      if (ev.state === "starting") caption("Waking up…", false);
      if (ev.state === "listening") { hideCard(); caption(""); }
      if (ev.state === "idle") { J.cardHideAt = now + 6000; setTimeout(() => J.mode === "idle" && caption(""), 2500); }
      break;
    case "wake":
      emote("surprised", 0.5);
      blink(true);
      break;
    case "level":
      J.spk = Math.min(1, (ev.spk || 0) / 0.11);
      J.mic = Math.min(1, (ev.mic || 0) / 0.05);
      break;
    case "doa":
      // While Jansky talks, the array hears its own speaker: ignore that direction.
      if (ev.speaking && J.mode !== "speaking") {
        const a = (ev.angle - J.doaFront) * Math.PI / 180;
        S.gx.t = J.doaSign * Math.sin(a) * 58;
        S.gy.t = 4;
        J.lookUntil = now + 2500;
        J.lastActive = now;
      }
      break;
    case "heard":
      caption(ev.text, true);
      break;
    case "speak": {
      caption(ev.text, false);
      const first = ev.text.toLowerCase().replace(/[^a-z ]/g, "").trim();
      for (const [w, mood] of Object.entries(OPENER_MOOD)) {
        if (first.startsWith(w) && mood) { emote(mood, 2.2); break; }
      }
      break;
    }
    case "emotion":
      emote(ev.name, ev.seconds || 2.4);
      break;
    case "card":
      showCard(ev);
      break;
    case "timers":
      J.timers = ev.items || [];
      renderTimers();
      break;
    case "todo":
      J.todo = { items: ev.items || [], shown: !!ev.shown };
      renderTodo(ev.highlight);
      break;
    case "muted":
      J.muted = !!ev.on;
      mutedEl.hidden = !J.muted;
      caption(J.muted ? "Muted. I won't wake to my name. Hold the screen again to unmute." : "Listening for my name again.", false);
      captionTimer = setTimeout(() => caption(""), 3500);
      break;
  }
}

// ---------------------------------------------------------------- captions and cards

let captionTimer = null;
function caption(text, heard) {
  clearTimeout(captionTimer);
  if (!J.captions || !text) { captionEl.classList.remove("shown"); return; }
  captionEl.textContent = text;
  captionEl.classList.toggle("heard", !!heard);
  captionEl.classList.add("shown");
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function dialSVG(fraction) {
  const c = 2 * Math.PI * 46;
  return `<svg viewBox="0 0 100 100" aria-hidden="true">
      <circle class="track" cx="50" cy="50" r="46" fill="none" stroke-width="5"/>
      <circle class="arc" cx="50" cy="50" r="46" fill="none" stroke-width="5"
        stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - fraction)}"/></svg>`;
}

function clock(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
}

function cap(s) { return s ? s[0].toUpperCase() + s.slice(1) : s; }

function showCard(c) {
  let html = "";
  if (c.kind === "weather") {
    html = `<div class="big">${Math.round(c.temp)}<sup>°</sup></div>
            <div class="what">${esc(cap(c.sky))}</div>
            <div class="where">${esc(c.city)}. ${esc(c.detail)}</div>`;
  } else if (c.kind === "news") {
    html = `<ol class="headlines">${c.headlines.slice(0, 3).map(h => `<li>${esc(h)}</li>`).join("")}</ol>`;
  } else if (c.kind === "timer") {
    html = `<div class="dial" data-id="${c.id ?? ""}" data-end="${c.end}" data-total="${c.total}">${dialSVG(1)}
              <div class="readout">${clock(c.total)}<small>${esc(c.label)}</small></div></div>
            <div class="where">Tap the dial to stop it</div>`;
  } else if (c.kind === "fact" || c.kind === "memory") {
    html = `<div class="fact-title">${esc(c.title)}</div><p class="fact-text">${esc(c.text)}</p>`;
  } else if (c.kind === "clock") {
    html = `<div class="big">${esc(c.time)}</div><div class="what">${esc(c.day)}</div>`;
  } else if (c.kind === "home") {
    const many = c.devices.length > 1;
    html = `<div class="devices${many ? " many" : ""}">${c.devices.map(d => `
      <div class="device${d.on ? " on" : ""}">${ICONS[d.icon] || ICONS.plug}
        <div class="what">${esc(d.name)}</div><div class="state">${d.online === false ? "Offline" : d.on ? "On" : "Off"}</div></div>`).join("")}</div>`;
  } else if (c.kind === "system") {
    html = `<div class="big">${Math.round(c.cpu)}<sup>°</sup></div>
            <div class="what">${esc(cap(c.mood))}</div>
            <div class="where">${esc(c.detail)}</div>`;
  } else {
    return;
  }
  cardEl.innerHTML = html;
  cardEl.hidden = false;
  cardEl.dataset.kind = c.kind;
  setTimeout(() => cardEl.classList.add("shown"), 220);  // let the face clear the space first
  J.card = c;
  J.cardHideAt = Infinity;
  aside();
}

function hideCard() {
  if (!J.card) return;
  J.card = null;
  cardEl.classList.remove("shown");
  setTimeout(() => { if (!J.card) cardEl.hidden = true; }, 520);
  setTimeout(aside, J.todo.shown ? 380 : 0);  // the list comes back once the card has gone
}

// The right half holds a card if there is one, else the list if it is open; the eyes step aside for either.
function listShown() { return !J.card && J.todo.shown && J.todo.items.length > 0; }

function aside() {
  const list = listShown();
  if (list && todoEl.hidden) {
    // Unhide first and add .shown a frame later, or the slide-in never runs and the list pops over the right eye.
    todoEl.hidden = false;
    requestAnimationFrame(() => requestAnimationFrame(() => todoEl.classList.toggle("shown", listShown())));
  } else {
    todoEl.classList.toggle("shown", list);
  }
  badgeEl.hidden = J.todo.shown || !J.todo.items.length;
  S.faceX.t = J.card || list ? -170 : 0;
  S.scale.t = J.card || list ? 0.8 : 1;
}

// ---------------------------------------------------------------- the list

const LIST_ICON = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6l1.5 1.5L8 5M4 12l1.5 1.5L8 11M4 18l1.5 1.5L8 17M11 6.5h9M11 12.5h9M11 18.5h9"/></svg>`;
const ICONS = {
  fan: `<svg viewBox="0 0 100 100" aria-hidden="true"><circle class="line" cx="50" cy="50" r="44"/>
    <g class="blades">${[0, 120, 240].map(a => `<path class="fill" transform="rotate(${a} 50 50)"
      d="M50 50 C 44 38, 40 22, 50 12 C 62 16, 62 34, 50 50 Z"/>`).join("")}</g>
    <circle class="line" cx="50" cy="50" r="5"/></svg>`,
  light: `<svg viewBox="0 0 100 100" aria-hidden="true">
    <g class="rays line">${[-60, -30, 0, 30, 60].map(a => `<path transform="rotate(${a} 50 44)" d="M50 6 V0"/>`).join("")}</g>
    <path class="fill line" d="M50 16 C 32 16, 24 30, 26 42 C 28 54, 38 58, 39 70 H 61 C 62 58, 72 54, 74 42 C 76 30, 68 16, 50 16 Z"/>
    <path class="line" d="M40 78 H60 M42 86 H58"/></svg>`,
  plug: `<svg viewBox="0 0 100 100" aria-hidden="true"><rect class="fill line" x="22" y="30" width="56" height="40" rx="12"/>
    <path class="line" d="M38 30 V16 M62 30 V16 M50 70 V88"/></svg>`,
};

function renderTodo(highlight) {
  const items = J.todo.items;
  todoItemsEl.innerHTML = items.slice(0, 5).map(it => `
    <li data-id="${esc(it.id)}" class="${it.fired ? "due" : ""}${it.id === highlight ? " new" : ""}">
      <span class="tick" aria-hidden="true"></span>
      <span class="text">${esc(it.text)}</span>${it.when ? `<span class="when">${esc(it.when)}</span>` : "<span></span>"}
    </li>`).join("");
  todoMoreEl.textContent = items.length > 5 ? `+ ${items.length - 5} more` : "";
  badgeEl.innerHTML = `${LIST_ICON}<span>${items.length}</span>`;
  aside();
}

function post(path, body) {
  if (DEMO) return;
  fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
                body: body ? JSON.stringify(body) : undefined }).catch(() => {});
}

// Tap an item: the tick fills, the line is struck through, then it folds away and Jansky forgets it.
todoItemsEl.addEventListener("click", e => {
  const li = e.target instanceof Element && e.target.closest("li[data-id]");
  if (!li || li.classList.contains("done")) return;
  li.classList.add("done");
  setTimeout(() => {
    if (DEMO) { handle({ type: "todo", items: J.todo.items.filter(it => it.id !== li.dataset.id), shown: J.todo.shown }); return; }
    post("/todo/done", { id: li.dataset.id });
  }, 750);
});
// Tap a timer dial (the small ones top right, or the big one on the card) to stop that timer.
function stopTimer(e) {
  const dial = e.target instanceof Element && e.target.closest(".dial[data-id]");
  if (!dial || dial.dataset.id === "" || dial.classList.contains("stopping")) return;
  dial.classList.add("stopping");
  if (DEMO) { setTimeout(() => handle({ type: "timers", items: J.timers.filter(t => String(t.id) !== dial.dataset.id) }), 350); return; }
  post("/timer/cancel", { id: +dial.dataset.id });
}
timersEl.addEventListener("click", stopTimer);
cardEl.addEventListener("click", stopTimer);

document.getElementById("todo-close").addEventListener("click", () => {
  J.todo.shown = false;
  aside();
  post("/todo/hide");
});
badgeEl.addEventListener("click", () => {
  J.todo.shown = true;
  aside();
  post("/todo/show");
});

function renderTimers() {
  timersEl.innerHTML = J.timers.map(t =>
    `<div class="dial" role="button" aria-label="Stop the ${esc(t.label)} timer" data-id="${t.id}" data-end="${t.end}" data-total="${t.total}">${dialSVG(1)}<div class="readout">--<small></small></div></div>`
  ).join("");
  // A timer that is no longer running takes its big card dial with it.
  if (J.card && J.card.kind === "timer" && !J.timers.some(t => t.id === J.card.id)) hideCard();
  tickDials();
}

function tickDials() {
  const now = Date.now() / 1000;
  for (const d of document.querySelectorAll(".dial[data-end]")) {
    const end = +d.dataset.end, total = +d.dataset.total;
    const left = Math.max(0, end - now);
    const arc = d.querySelector(".arc");
    const c = 2 * Math.PI * 46;
    arc.setAttribute("stroke-dashoffset", c * (1 - left / total));
    d.querySelector(".readout").firstChild.nodeValue = clock(left);
  }
}
setInterval(tickDials, 250);

// ---------------------------------------------------------------- the sky

const sky = document.createElement("canvas");
sky.width = W; sky.height = H;
{
  const s = sky.getContext("2d");
  const g = s.createRadialGradient(W / 2, H * 0.42, 40, W / 2, H * 0.42, 560);
  g.addColorStop(0, "#1a1f48");
  g.addColorStop(0.55, "#0e1230");
  g.addColorStop(1, "#070b1a");
  s.fillStyle = g;
  s.fillRect(0, 0, W, H);
}

const stars = Array.from({ length: 150 }, () => ({
  x: Math.random() * W, y: Math.random() * H,
  r: 0.35 + Math.random() ** 3 * 1.2,
  a: 0.18 + Math.random() * 0.55,
  ph: Math.random() * Math.PI * 2,
  depth: 0.3 + Math.random() * 0.7,
}));

const rings = [];
let ringClock = 0;

// ---------------------------------------------------------------- eyes

let blinkT = -1, nextBlink = 2;
function blink(force) { if (force || blinkT < 0) blinkT = 0; }

function blinkMul() {
  if (blinkT < 0) return 1;
  const d = 0.15;
  if (blinkT > d) { blinkT = -1; return 1; }
  return 1 - Math.sin(Math.PI * blinkT / d) * 0.94;
}

// Each eye is drawn with its lids into its own small canvas, then stamped onto the sky with a glow.
// Glowing the finished shape (instead of clipping a glowing shape) keeps the bloom on the true outline.
const EYE_BOX = 320;
const eyeCanvas = document.createElement("canvas");
eyeCanvas.width = eyeCanvas.height = EYE_BOX * dpr;
const ec = eyeCanvas.getContext("2d");
ec.scale(dpr, dpr);

function drawEye(cx, cy, side, k) {
  const sc = S.scale.v;
  // Looking sideways narrows the far eye and widens the near one, which reads as depth.
  const turn = S.gx.v / 70;
  const persp = 1 + 0.12 * turn * side;
  const w = S.w.v * sc * persp, h = S.h.v * sc * k * (1 + 0.05 * turn * side);
  const r = Math.min(S.r.v * sc, w / 2, h / 2);
  const glow = S.glow.v;
  const o = EYE_BOX / 2;

  ec.clearRect(0, 0, EYE_BOX, EYE_BOX);
  ec.globalCompositeOperation = "source-over";
  const g = ec.createRadialGradient(o, o - h * 0.12, 6, o, o, Math.max(w, h) * 0.78);
  g.addColorStop(0, "#fff3d6");
  g.addColorStop(0.32, "#ffc46b");
  g.addColorStop(1, "#ff8f3f");
  ec.fillStyle = g;
  ec.beginPath();
  ec.roundRect(o - w / 2, o - h / 2, w, h, r);
  ec.fill();

  // A faint filament: what makes these vacuum-tube eyes rather than LED eyes.
  ec.globalAlpha = 0.28 * glow;
  ec.strokeStyle = "#fffaf0";
  ec.lineWidth = 2;
  ec.beginPath();
  const fh = h * 0.42;
  ec.moveTo(o - w * 0.12, o + fh / 2);
  ec.bezierCurveTo(o - w * 0.12, o - fh / 2, o + w * 0.12, o - fh / 2, o + w * 0.12, o + fh / 2);
  ec.stroke();
  ec.globalAlpha = 1;

  // Lids: erase above the top lid line, and below the smile curve.
  ec.globalCompositeOperation = "destination-out";
  if (S.lidTop.v > 0.005) {
    const ly = o - h / 2 + S.lidTop.v * h;
    const tilt = S.lidTilt.v * h * 0.5;
    // Positive tilt drops the inner corners (focused), negative drops the outer ones (sad).
    const yInner = ly + tilt, yOuter = ly - tilt;
    const yL = side < 0 ? yOuter : yInner, yR = side < 0 ? yInner : yOuter;
    // The lid edge sags in the middle like a real eyelid, so a half-closed eye is not a flat-topped box.
    const sag = w * 0.16 * Math.min(1, S.lidTop.v * 4);
    const slope = (yR - yL) / w;
    ec.beginPath();
    ec.moveTo(0, 0); ec.lineTo(EYE_BOX, 0);
    ec.lineTo(EYE_BOX, yR + slope * (EYE_BOX - o - w / 2));
    ec.lineTo(o + w / 2, yR);
    ec.quadraticCurveTo(o, (yL + yR) / 2 + sag, o - w / 2, yL);
    ec.lineTo(0, yL - slope * (o - w / 2));
    ec.closePath();
    ec.fill();
  }
  if (S.lidBottom.v > 0.01) {
    ec.beginPath();
    // A narrow, deep cut from below turns the eye into an upturned crescent: the smiling eye.
    ec.ellipse(o, o + h / 2 + h * 0.58 - S.lidBottom.v * h * 1.15, w * 0.5, h * 0.6, 0, 0, Math.PI * 2);
    ec.fill();
  }
  ec.globalCompositeOperation = "source-over";

  ctx.save();
  ctx.shadowColor = `rgba(255, 138, 61, ${0.8 * glow})`;
  ctx.shadowBlur = 42 * glow;
  ctx.globalAlpha = Math.min(1, 0.45 + 0.55 * glow);
  ctx.drawImage(eyeCanvas, cx - o, cy - o, EYE_BOX, EYE_BOX);
  ctx.restore();
}

function drawTrace(t, cx, cy) {
  const n = 80, wd = 220 * S.scale.v, x0 = cx - wd / 2;
  const amp = S.trace.v;
  const thinking = J.mode === "thinking";
  const ph = ((t * 0.9) % 1.3) - 0.15;
  ctx.save();
  ctx.beginPath();
  for (let i = 0; i <= n; i++) {
    const u = i / n;
    const env = Math.sin(Math.PI * u) ** 1.4;
    let y = (Math.sin(u * 23 + t * 15) * 0.6 + Math.sin(u * 41 - t * 10) * 0.28 + Math.sin(u * 8 + t * 6) * 0.45) * amp * env;
    if (thinking) y -= 11 * Math.exp(-((u - ph) ** 2) / 0.0035);
    i ? ctx.lineTo(x0 + u * wd, cy + y) : ctx.moveTo(x0 + u * wd, cy + y);
  }
  const lit = Math.min(1, 0.32 + amp / 14 + (thinking ? 0.25 : 0));
  ctx.strokeStyle = `rgba(255, 196, 107, ${lit})`;
  ctx.lineWidth = 3;
  ctx.lineJoin = "round";
  ctx.shadowColor = "rgba(255, 138, 61, 0.8)";
  ctx.shadowBlur = 14 * lit;
  ctx.stroke();
  ctx.restore();
}

// ---------------------------------------------------------------- behaviour

let saccadeAt = 0;
function behave(now, dt) {
  // Expression: a passing emotion wins, then the mode, then how long it has been idle.
  let expr = { idle: "neutral", listening: "listening", thinking: "thinking", speaking: "speaking",
               starting: "sleepy" }[J.mode] || "neutral";
  if (J.mode === "idle") {
    const idleFor = (now - J.lastActive) / 1000;
    if (idleFor > 600) expr = "asleep";
    else if (idleFor > 300) expr = "sleepy";
  }
  if (J.emotion && now < J.emotionUntil) expr = J.emotion;
  setExpr(expr);

  // Gaze
  if (now < J.lookUntil) {
    // keep looking at whoever is talking (set by DoA)
  } else if (J.mode === "thinking") {
    S.gx.t = 30 + Math.sin(now / 420) * 14;
    S.gy.t = -26;
  } else if (J.mode === "listening" || J.mode === "speaking") {
    if (now > saccadeAt) {
      S.gx.t = (Math.random() - 0.5) * 16;
      S.gy.t = (Math.random() - 0.5) * 8;
      saccadeAt = now + 900 + Math.random() * 1600;
    }
  } else if (now > saccadeAt && expr !== "asleep") {
    const centre = Math.random() < 0.4;
    S.gx.t = centre ? 0 : (Math.random() - 0.5) * 90;
    S.gy.t = centre ? 0 : (Math.random() - 0.5) * 40;
    saccadeAt = now + 1500 + Math.random() * 3200;
  }

  // Blinks: never while asleep, a double blink now and then.
  nextBlink -= dt;
  if (nextBlink <= 0 && expr !== "asleep") {
    blink();
    if (Math.random() < 0.2) setTimeout(() => blink(), 260);
    nextBlink = 2.2 + Math.random() * 4.5;
  }
  if (blinkT >= 0) blinkT += dt;

  // Mouth and pulse follow the voice; rings follow the room.
  S.trace.t = J.mode === "speaking" ? 3 + 24 * J.spk : J.mode === "listening" ? 1 + 7 * J.mic : 0.8;
  if (!J.card && !listShown()) S.scale.t = 1 + (J.mode === "speaking" ? 0.035 * J.spk : 0);
  S.rings.t = J.mode === "listening" ? 0.4 + 0.6 * J.mic : 0;

  if (J.card && now > J.cardHideAt) hideCard();
}

// ---------------------------------------------------------------- frame

let last = performance.now();
function frame(now) {
  // 30 fps while idle (blinks and glances look the same), 60 fps when there is something to react to.
  // On a hot, throttled Pi the full-rate glow cost most of a core.
  if (J.mode === "idle" && !J.card && now - last < 30) { requestAnimationFrame(frame); return; }
  const dt = Math.min(0.05, (now - last) / 1000);
  last = now;
  const t = now / 1000;
  behave(now, dt);
  for (const s of Object.values(S)) s.step(dt);

  ctx.drawImage(sky, 0, 0);

  // stars, drifting slightly against the gaze for depth
  for (const s of stars) {
    const tw = reduceMotion ? 1 : 0.65 + 0.35 * Math.sin(t * 0.8 + s.ph);
    ctx.globalAlpha = s.a * tw;
    ctx.fillStyle = "#dfe2ff";
    ctx.beginPath();
    ctx.arc(s.x - S.gx.v * 0.12 * s.depth - S.faceX.v * 0.05 * s.depth, s.y - S.gy.v * 0.1 * s.depth, s.r, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalAlpha = 1;

  const cx = W / 2 + S.faceX.v, cy = 200;
  const spread = 268 * S.scale.v;
  const ex = cx + S.gx.v, ey = cy + S.gy.v;

  // radio rings while listening, like a dish receiving
  if (!reduceMotion) {
    ringClock -= dt;
    if (S.rings.v > 0.05 && ringClock <= 0) {
      rings.push({ r: 70, a: S.rings.v });
      ringClock = 0.55 - 0.3 * J.mic;
    }
    for (let i = rings.length - 1; i >= 0; i--) {
      const ring = rings[i];
      ring.r += dt * 170;
      const fade = 1 - (ring.r - 70) / 360;
      if (fade <= 0) { rings.splice(i, 1); continue; }
      ctx.strokeStyle = `rgba(255, 196, 107, ${0.22 * ring.a * fade})`;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(ex, ey + 10, ring.r, 0, Math.PI * 2);
      ctx.stroke();
    }
  }

  const k = blinkMul();
  drawEye(ex - spread / 2, ey, -1, k);
  drawEye(ex + spread / 2, ey, 1, k);
  drawTrace(t, cx + S.gx.v * 0.5, 368 + S.gy.v * 0.35 - (1 - S.scale.v) * 60);

  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ---------------------------------------------------------------- input and connection

// A tap wakes Jansky (or interrupts it). A swipe down reveals the Close button.
const controlsEl = document.getElementById("controls");
const closeBtn = document.getElementById("close");
let gesture = null, controlsTimer = null;

function showControls(on) {
  clearTimeout(controlsTimer);
  controlsEl.classList.toggle("shown", on);
  controlsEl.setAttribute("aria-hidden", String(!on));
  closeBtn.tabIndex = on ? 0 : -1;
  if (on) { closeBtn.focus({ preventScroll: true }); controlsTimer = setTimeout(() => showControls(false), 6000); }
}

// Hold still for 0.7 s: mute or unmute the wake word (so narration that says "Jansky" doesn't wake it).
const OWN_TOUCH = "#controls, #todo, #todo-badge, #timers .dial, #card .dial";
addEventListener("pointerdown", e => {
  const own = e.target instanceof Element && e.target.closest(OWN_TOUCH);
  gesture = { x: e.clientX, y: e.clientY, long: false };
  if (own) return;
  const g = gesture;
  g.timer = setTimeout(() => {
    if (gesture !== g) return;
    g.long = true;
    if (DEMO) handle({ type: "muted", on: !J.muted }); else post("/mute");
  }, 700);
});
addEventListener("pointermove", e => {
  if (gesture && Math.hypot(e.clientX - gesture.x, e.clientY - gesture.y) > 20) clearTimeout(gesture.timer);
});
addEventListener("pointerup", e => {
  if (gesture) clearTimeout(gesture.timer);
  if (!gesture || gesture.long || (e.target instanceof Element && e.target.closest(OWN_TOUCH))) { gesture = null; return; }
  const scale = stage.getBoundingClientRect().height / H;
  const dx = (e.clientX - gesture.x) / scale, dy = (e.clientY - gesture.y) / scale;
  gesture = null;
  if (dy > 60 && dy > Math.abs(dx) * 1.5) { showControls(true); return; }
  if (Math.abs(dx) > 20 || Math.abs(dy) > 20) return;  // some other drag: ignore
  if (controlsEl.classList.contains("shown")) { showControls(false); return; }
  emote("surprised", 0.6);
  J.lastActive = performance.now();
  post("/tap");
});
addEventListener("keydown", e => {
  if (e.key === "Escape") showControls(!controlsEl.classList.contains("shown"));
});
closeBtn.addEventListener("click", () => {
  showControls(false);
  caption("Closing Jansky…", false);
  emote("sleepy", 10);
  post("/quit");
});

const params = new URLSearchParams(location.search);
const DEMO = params.has("demo");
if (params.has("expr")) emote(params.get("expr"), 1e9);  // ?expr=happy holds one expression, for tuning
if (!DEMO) {
  const es = new EventSource("/events");
  es.onmessage = e => handle(JSON.parse(e.data));
} else {
  runDemo();
}

// ---------------------------------------------------------------- demo loop (?demo)

function runDemo() {
  let speakingLevel = null;
  const talk = (seconds) => {
    clearInterval(speakingLevel);
    const until = performance.now() + seconds * 1000;
    speakingLevel = setInterval(() => {
      const on = performance.now() < until;
      handle({ type: "level", spk: on ? 0.05 + Math.random() * 0.07 * (Math.sin(performance.now() / 90) > -0.3) : 0, mic: 0 });
      if (!on) clearInterval(speakingLevel);
    }, 40);
  };
  const listen = () => {
    const iv = setInterval(() => handle({ type: "level", spk: 0, mic: 0.01 + Math.random() * 0.04 }), 60);
    return () => clearInterval(iv);
  };
  const pen = { id: "pen", text: "Buy a pen", when: "Today", due: null, fired: false };
  const bank = { id: "bank", text: "Call the bank", when: "3:00 PM", due: null, fired: true };
  const steps = [
    [0, () => { handle({ type: "state", state: "idle" }); handle({ type: "todo", items: [], shown: false }); }],
    [1500, () => { handle({ type: "wake" }); handle({ type: "state", state: "listening" }); demoStop = listen(); }],
    [1800, () => { demoStop(); handle({ type: "heard", text: "Remind me to buy a pen today." }); handle({ type: "state", state: "thinking" }); }],
    [400, () => {
      handle({ type: "state", state: "speaking" });
      handle({ type: "speak", text: "Got it. I'll remind you to buy a pen today." });
      handle({ type: "todo", items: [pen, bank], shown: true, highlight: "pen" });
      talk(3);
    }],
    [3400, () => handle({ type: "state", state: "idle" })],
    [2500, () => { handle({ type: "wake" }); handle({ type: "state", state: "listening" }); demoStop = listen(); }],
    [1600, () => { demoStop(); handle({ type: "heard", text: "Turn on the fan." }); handle({ type: "state", state: "thinking" }); }],
    [300, () => {
      handle({ type: "state", state: "speaking" });
      handle({ type: "speak", text: "Sure thing. The fan is on." });
      handle({ type: "card", kind: "home", devices: [{ name: "Fan", icon: "fan", on: true, online: true }] });
      talk(2);
    }],
    [2400, () => handle({ type: "state", state: "idle" })],
    [2500, () => { handle({ type: "wake" }); handle({ type: "state", state: "listening" }); demoStop = listen(); }],
    [1600, () => { demoStop(); handle({ type: "heard", text: "Good night." }); handle({ type: "state", state: "thinking" }); }],
    [300, () => {
      handle({ type: "state", state: "speaking" });
      handle({ type: "speak", text: "Okay, good night, Mayukh. The desk light and the fan are off. Sleep well." });
      handle({ type: "card", kind: "home", devices: [{ name: "Desk light", icon: "light", on: false, online: true }, { name: "Fan", icon: "fan", on: false, online: true }] });
      talk(4);
    }],
    [4400, () => { handle({ type: "state", state: "idle" }); handle({ type: "emotion", name: "sleepy", seconds: 5 }); }],
    [6000, () => {}],
    [3000, () => { handle({ type: "wake" }); handle({ type: "state", state: "listening" }); handle({ type: "doa", angle: 300, speaking: true }); demoStop = listen(); }],
    [2400, () => { demoStop(); handle({ type: "heard", text: "What's the weather tomorrow?" }); handle({ type: "state", state: "thinking" }); }],
    [900, () => {
      handle({ type: "state", state: "speaking" });
      handle({ type: "speak", text: "Let me check. Tomorrow in Kingston: clear skies, a high of 13." });
      handle({ type: "card", kind: "weather", temp: 13, sky: "clear skies", city: "Kingston", detail: "Tomorrow, low of 1, no rain expected." });
      talk(5);
    }],
    [5200, () => handle({ type: "state", state: "idle" })],
    [2500, () => { handle({ type: "wake" }); handle({ type: "state", state: "listening" }); handle({ type: "doa", angle: 60, speaking: true }); demoStop = listen(); }],
    [2000, () => { demoStop(); handle({ type: "heard", text: "Tell me a joke about radio telescopes." }); handle({ type: "state", state: "thinking" }); }],
    [500, () => { handle({ type: "state", state: "speaking" }); handle({ type: "speak", text: "Ha! Why did the photon refuse to check a bag? It was travelling light." }); talk(4.5); }],
    [4800, () => handle({ type: "state", state: "idle" })],
    [2000, () => { handle({ type: "wake" }); handle({ type: "state", state: "listening" }); demoStop = listen(); }],
    [1800, () => { demoStop(); handle({ type: "heard", text: "Set a timer for twelve minutes for the pasta." }); handle({ type: "state", state: "thinking" }); }],
    [400, () => {
      const end = Date.now() / 1000 + 720;
      handle({ type: "state", state: "speaking" });
      handle({ type: "speak", text: "Sure thing. Twelve minute pasta timer, starting now." });
      handle({ type: "card", kind: "timer", id: 7, label: "pasta", end, total: 720 });
      handle({ type: "timers", items: [{ id: 7, label: "pasta", end, total: 720 }] });
      talk(3.5);
    }],
    [3800, () => handle({ type: "state", state: "idle" })],
    [3000, () => { handle({ type: "wake" }); handle({ type: "state", state: "listening" }); demoStop = listen(); }],
    [1800, () => { demoStop(); handle({ type: "heard", text: "Tell me some news." }); handle({ type: "state", state: "thinking" }); }],
    [500, () => {
      handle({ type: "state", state: "speaking" });
      handle({ type: "speak", text: "Here are the top headlines." });
      handle({ type: "card", kind: "news", headlines: [
        "SpaceX will attempt its first double Starship booster catch on the next test flight",
        "Satellite images reveal the scale of the Wainwright fire",
        "New lawsuit challenges tariffs on Canadian food imports"] });
      talk(6);
    }],
    [6400, () => handle({ type: "state", state: "idle" })],
    [7000, () => runDemo()],
  ];
  let demoStop = () => {};
  let at = 0;
  for (const [delay, fn] of steps) { at += delay; setTimeout(fn, at); }
}
