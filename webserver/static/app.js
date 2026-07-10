import { DiscordSDK } from "./vendor/discord-sdk.js";
import { startRain, startTrail } from "./sbuga.js";

// Inside the activity iframe all XHR/fetch goes through Discord's proxy
// prefix; hit directly (testing), the server has no such prefix.
const EMBEDDED =
  location.hostname.endsWith("discordsays.com") ||
  new URLSearchParams(location.search).has("frame_id");
const API = EMBEDDED ? "/.proxy" : "";

// the server injects window.__BUILD (the app.js/style.css content hash) into
// index.html, rendered bottom-right so you can confirm the live build at a glance
const APP_VERSION = (typeof window !== "undefined" && window.__BUILD) || "dev";

let accessToken = null;
let currentMode = null;
let currentRound = null; // active (unfinished) round, else null
let timerHandle = null;
let theme = "dark";
let appName = "SYSbuga";

// --- spectate / presence state ---
let hubWs = null;
let selfId = null; // our user id as a string snowflake (JS numbers lose precision)
let selfName = "Player";
let selfAvatar = null; // discord avatar hash (or null)
let instanceId = ""; // discord activity instance id (the room key)
let roomMembers = []; // [{id, name, avatar, active}] everyone connected to this instance
let spectateTarget = null; // id we're currently watching, or null
let spectateName = ""; // display name of the current/last spectate target
let pendingSpectate = null; // target that left; auto-resume if they rejoin
let spectateTimerHandle = null;
let hubClosing = false;
let heartbeatHandle = null;
// what we're currently broadcasting, replayed verbatim after a reconnect so a
// dropped socket doesn't reset us to "not playing" or lose our spectators
let lastRoundPayload = null; // the active/last round, or null when not in a round
let myGuessLog = []; // our own log entries for this round
let myResult = null; // reveal result once the round ends, else null

const $ = (id) => document.getElementById(id);

const TITLES = { setup: "Guessing" };

function show(screen) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.remove("active"));
  $(`screen-${screen}`).classList.add("active");

  const bar = $("topbar");
  if (screen === "loading" || screen === "round" || screen === "spectate") {
    bar.hidden = true;
  } else {
    bar.hidden = false;
    $("topbar-title").textContent = TITLES[screen] || appName;
    $("topbar-back").hidden = screen === "home";
  }
}

function openSettings() {
  $("settings-modal").hidden = false;
}
function closeSettings() {
  $("settings-modal").hidden = true;
}

async function api(path, options = {}) {
  const resp = await fetch(`${API}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      ...(options.headers || {}),
    },
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail || detail;
    } catch {}
    throw new Error(detail);
  }
  return resp.json();
}

// --- confirm modal ---

function confirmModal(text, confirmText = "Confirm", cancelText = "Cancel") {
  $("modal-text").textContent = text;
  $("modal-confirm").textContent = confirmText;
  $("modal-cancel").textContent = cancelText;
  $("modal").hidden = false;
  return new Promise((resolve) => {
    const done = (val) => {
      $("modal").hidden = true;
      $("modal-confirm").onclick = null;
      $("modal-cancel").onclick = null;
      resolve(val);
    };
    $("modal-confirm").onclick = () => done(true);
    $("modal-cancel").onclick = () => done(false);
  });
}

// --- theme ---

function applyTheme() {
  document.body.classList.toggle("light", theme === "light");
  $("theme-toggle").checked = theme === "dark";
}

async function toggleTheme() {
  theme = $("theme-toggle").checked ? "dark" : "light";
  applyTheme();
  try {
    await api("/api/activity/settings", {
      method: "POST",
      body: JSON.stringify({ theme }),
    });
  } catch {}
}

// --- boot / auth ---

function bootError(msg) {
  const bar = document.querySelector(".progress");
  if (bar) bar.hidden = true;
  $("loading-error").hidden = false;
  $("loading-error").textContent = msg;
}

// `prompt: "none"` returns a code silently for already-authorized users (no
// consent screen each launch); fall back to the interactive consent the first
// time, when there's nothing to silently reuse.
async function authorize(sdk, clientId) {
  const params = {
    client_id: clientId,
    response_type: "code",
    state: "",
    scope: ["identify"],
  };
  try {
    return await sdk.commands.authorize({ ...params, prompt: "none" });
  } catch {
    return await sdk.commands.authorize(params);
  }
}

async function boot() {
  const config = await api("/api/config");
  if (config.name) {
    appName = config.name;
    document.title = appName;
    $("app-name").textContent = appName;
    document.querySelector(".home-title").textContent = appName;
  }

  if (!EMBEDDED) {
    bootError("Server is up! Open this as an activity inside Discord to play.");
    return;
  }
  if (!config.client_id) {
    bootError("Server isn't configured yet (no client id).");
    return;
  }

  const sdk = new DiscordSDK(config.client_id);
  await sdk.ready();

  const { code } = await authorize(sdk, config.client_id);

  const token = await api("/api/oauth/token", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
  accessToken = token.access_token;
  const auth = await sdk.commands.authenticate({ access_token: accessToken });
  if (auth && auth.user) {
    selfId = String(auth.user.id);
    selfName = auth.user.global_name || auth.user.username || "Player";
    selfAvatar = auth.user.avatar || null;
  }
  instanceId = sdk.instanceId || "";
  connectHub();
  initPresence(sdk).catch(() => {}); // best-effort; the room is the real source

  try {
    const settings = await api("/api/activity/settings");
    theme = settings.theme === "light" ? "light" : "dark";
  } catch {}
  applyTheme();

  const modes = await api("/api/activity/modes");
  const select = $("mode-select");
  for (const mode of modes) {
    const option = document.createElement("option");
    option.value = mode.value;
    option.textContent = mode.label;
    select.appendChild(option);
  }

  show("home");
}

// --- round ---

function stopTimer() {
  if (timerHandle) clearInterval(timerHandle);
  timerHandle = null;
}

function startTimer(expiresAt) {
  stopTimer();
  const el = $("round-timer");
  el.hidden = false;
  const tick = () => {
    const left = Math.max(0, expiresAt - Date.now() / 1000);
    el.textContent = `${Math.ceil(left)}s`;
    el.classList.toggle("low", left <= 10);
    if (left <= 0) {
      stopTimer();
      timeUp();
    }
  };
  tick();
  timerHandle = setInterval(tick, 250);
}

function setResult(text, cls) {
  const el = $("round-result");
  el.textContent = text;
  el.className = cls || "";
}

function setFormEnabled(on) {
  $("guess-input").disabled = !on;
  $("guess-submit").disabled = !on;
  $("btn-hint").disabled = !on;
}

function appendLog(logEl, icon, text, cls) {
  const li = document.createElement("li");
  li.className = cls;
  li.innerHTML = `<span class="marker"></span><span class="text"></span>`;
  li.querySelector(".marker").textContent = icon;
  li.querySelector(".text").textContent = text;
  logEl.append(li);
  logEl.scrollTop = logEl.scrollHeight;
}

// player's own guess log entry — mirror it to anyone spectating us
function playerLog(icon, text, cls) {
  const entry = { marker: icon, text, cls };
  appendLog($("guess-log"), icon, text, cls);
  myGuessLog.push(entry);
  hubSend({ op: "log", entry });
}

// round media is either a chart-clip video or a cropped image; swap the right element in
function setRoundMedia(imgId, videoId, round) {
  const img = $(imgId);
  const video = $(videoId);
  const url = `${API}/api/activity/guess/round/${round.round_id}/image`;
  if (round.image_media === "video/mp4") {
    img.hidden = true;
    img.removeAttribute("src");
    video.src = url;
    video.hidden = false;
    video.play().catch(() => {}); // sticky activation from the start click lets sound play
  } else {
    clearVideo(video);
    img.src = url;
    img.hidden = false;
  }
}

function clearVideo(video) {
  if (!video) return;
  video.hidden = true;
  try {
    video.pause();
  } catch {}
  video.removeAttribute("src");
  video.load();
}

function clearRoundMedia(imgId, videoId) {
  const img = $(imgId);
  img.hidden = true;
  img.removeAttribute("src");
  clearVideo($(videoId));
}

async function startRound(mode) {
  currentMode = mode;
  currentRound = null;
  show("round");
  const label = [...$("mode-select").options].find((o) => o.value === mode);
  $("round-mode").textContent = label ? label.textContent : "Guess";
  setResult("");
  $("guess-log").innerHTML = "";
  $("btn-again").hidden = true;
  $("guess-form").hidden = false;
  $("btn-giveup").hidden = true; // shown once the round is live
  clearRoundMedia("round-image", "round-video");
  $("round-timer").hidden = true; // no timer while loading
  $("round-prompt").textContent = "Loading…";
  $("guess-input").value = "";
  setFormEnabled(false); // guess bar disabled while loading

  let round;
  try {
    round = await api("/api/activity/guess/start", {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
  } catch (e) {
    $("round-prompt").textContent = `Couldn't start: ${e.message}`;
    $("guess-form").hidden = true;
    $("btn-again").hidden = false;
    lastRoundPayload = null;
    myGuessLog = [];
    myResult = null;
    hubSend({ op: "clear" });
    return;
  }

  currentRound = round;
  $("round-prompt").textContent = round.prompt || "";
  if (round.has_image) {
    setRoundMedia("round-image", "round-video", round);
  }
  setFormEnabled(true);
  $("btn-giveup").hidden = false;
  $("guess-input").focus();
  startTimer(round.expires_at);
  lastRoundPayload = {
    round_id: round.round_id,
    mode: round.mode,
    mode_label: $("round-mode").textContent,
    prompt: round.prompt,
    has_image: round.has_image,
    image_media: round.image_media,
    has_reveal: round.has_reveal,
    expires_at: round.expires_at,
  };
  myGuessLog = [];
  myResult = null;
  hubSend({ op: "round", round: lastRoundPayload });
}

function showReveal(round, message, cls) {
  stopTimer();
  currentRound = null;
  $("round-timer").hidden = true;
  setResult(message, cls);
  $("guess-form").hidden = true;
  $("btn-giveup").hidden = true;
  $("btn-hint").disabled = true;
  $("btn-again").hidden = false;
  // the guess log stays visible after the reveal on purpose
  if (round.has_reveal) {
    clearVideo($("round-video")); // reveal is always the full chart image
    const img = $("round-image");
    img.src = `${API}/api/activity/guess/round/${round.round_id}/reveal`;
    img.hidden = false;
  }
  myResult = {
    text: message,
    cls: cls || "",
    round_id: round.round_id,
    has_reveal: !!round.has_reveal,
  };
  hubSend({ op: "result", result: myResult });
}

async function submitGuess(event) {
  event.preventDefault();
  if (!currentRound) return;
  const guess = $("guess-input").value.trim();
  if (!guess) return;

  let result;
  try {
    result = await api("/api/activity/guess/submit", {
      method: "POST",
      body: JSON.stringify({ round_id: currentRound.round_id, guess }),
    });
  } catch (e) {
    setResult(e.message, "bad");
    return;
  }

  if (result.result === "correct") {
    // show what they typed, and what it resolved to when that differs (an alias)
    const text = guess === result.answer ? guess : `${guess} → ${result.answer}`;
    playerLog("✅", text, "right");
    const msg =
      result.time != null
        ? `Correct in ${result.time.toFixed(2)}s! It was ${result.answer}.`
        : `Correct! It was ${result.answer}.`;
    showReveal({ ...currentRound, ...result }, msg, "good");
  } else if (result.result === "expired") {
    showReveal({ ...currentRound, ...result }, `Time's up! It was ${result.answer}.`, "bad");
  } else if (result.result === "incorrect") {
    // their guess, the song it landed on, and the alias that matched (when the
    // alias isn't just the song's own name)
    const landed = result.matched_key
      ? `${result.matched} (${result.matched_key})`
      : result.matched;
    playerLog("❌", `${guess} → ${landed}`, "wrong");
    $("guess-input").value = "";
    hubSend({ op: "typing", text: "" });
    $("guess-input").focus();
  } else {
    playerLog("❔", guess, "miss");
    $("guess-input").select();
  }
}

async function reveal(round, prefix, cls) {
  try {
    const res = await api("/api/activity/guess/reveal", {
      method: "POST",
      body: JSON.stringify({ round_id: round.round_id }),
    });
    showReveal({ ...round, ...res }, `${prefix} It was ${res.answer}.`, cls);
  } catch {
    showReveal(round, prefix, cls);
  }
}

async function timeUp() {
  if (!currentRound) return;
  await reveal(currentRound, "Time's up!", "bad");
}

async function giveUp() {
  if (!currentRound) return;
  if (!(await confirmModal("Give up and reveal the answer?", "Give up", "Keep playing"))) return;
  if (!currentRound) return;
  await reveal(currentRound, "You gave up.", "bad");
}

async function useHint() {
  if (!currentRound) return;
  if (!(await confirmModal("Use a hint?", "Show hint", "Cancel"))) return;
  if (!currentRound) return;
  try {
    const res = await api("/api/activity/guess/hint", {
      method: "POST",
      body: JSON.stringify({ round_id: currentRound.round_id }),
    });
    playerLog("💡", `${res.hint}  (${res.length} chars)`, "hint");
  } catch (e) {
    setResult(e.message, "bad");
  }
}

// --- spectate hub (websocket) ---

function hubUrl() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${API}/api/activity/ws`;
}

function hubSend(obj) {
  if (hubWs && hubWs.readyState === WebSocket.OPEN) {
    try {
      hubWs.send(JSON.stringify(obj));
    } catch {}
  }
}

function connectHub() {
  if (!accessToken || !instanceId) return; // need auth + a room to join
  if (
    hubWs &&
    (hubWs.readyState === WebSocket.CONNECTING || hubWs.readyState === WebSocket.OPEN)
  )
    return; // don't stack sockets
  try {
    hubWs = new WebSocket(hubUrl());
  } catch {
    return;
  }
  hubWs.addEventListener("open", () => {
    hubSend({
      op: "hello",
      token: accessToken,
      instance_id: instanceId,
      name: selfName,
      avatar: selfAvatar,
    });
    // a reconnect re-runs join() server-side, which resets us to empty/not-playing,
    // so replay exactly what we're doing: watching someone, and/or our current round
    // (incl. the play-again screen, where lastRoundPayload is set but currentRound isn't)
    if (spectateTarget) hubSend({ op: "watch", target: spectateTarget });
    if (lastRoundPayload) {
      hubSend({ op: "round", round: lastRoundPayload });
      for (const entry of myGuessLog) hubSend({ op: "log", entry });
      if (myResult) hubSend({ op: "result", result: myResult });
    }
    startHeartbeat();
  });
  hubWs.addEventListener("message", (e) => {
    let msg;
    try {
      msg = JSON.parse(e.data);
    } catch {
      return;
    }
    onHubMessage(msg);
  });
  hubWs.addEventListener("close", (e) => {
    hubWs = null;
    stopHeartbeat();
    console.log("[hub] ws closed", e.code, e.reason || "");
    if (!hubClosing) setTimeout(connectHub, 2000); // reconnect
  });
  hubWs.addEventListener("error", () => {});
}

// keep the socket alive through Cloudflare/nginx idle timeouts (unknown ops are
// ignored server-side); the reconnect loop covers us if a drop slips through
function startHeartbeat() {
  stopHeartbeat();
  heartbeatHandle = setInterval(() => hubSend({ op: "ping" }), 30000);
}
function stopHeartbeat() {
  if (heartbeatHandle) clearInterval(heartbeatHandle);
  heartbeatHandle = null;
}

function onHubMessage(msg) {
  switch (msg.op) {
    case "ready":
      selfId = msg.you;
      break;
    case "members":
      roomMembers = msg.members || [];
      if (!$("spectate-modal").hidden) renderSpectateList();
      maybeResumeSpectate();
      break;
    case "watchers":
      renderWatchers(msg.watchers || []);
      break;
    case "snapshot":
      if (spectateTarget === msg.target) renderSnapshot(msg.state);
      break;
    case "round":
      if (spectateTarget === msg.from) spectateRound(msg.round);
      break;
    case "typing":
      if (spectateTarget === msg.from) spectateTyping(msg.text);
      break;
    case "log":
      if (spectateTarget === msg.from) appendLog($("spectate-log"), msg.entry.marker, msg.entry.text, msg.entry.cls);
      break;
    case "result":
      if (spectateTarget === msg.from) spectateResult(msg.result);
      break;
    case "clear":
      if (spectateTarget === msg.from) spectateCleared();
      break;
    case "watch_target_gone":
      if (spectateTarget === msg.target) spectateGone(msg.target);
      break;
  }
}

// if the person we were watching left and then rejoins the room, re-attach
function maybeResumeSpectate() {
  if (!pendingSpectate) return;
  const m = roomMembers.find((x) => x.id === pendingSpectate);
  if (!m) return;
  const id = pendingSpectate;
  pendingSpectate = null;
  startSpectating(id, m.name || spectateName);
}

// throttle live typing so we send at most ~1 update per 120ms (with a trailing send)
let typingTimer = null;
let lastTypingSent = 0;
function throttleTyping(text) {
  const gap = 120;
  const now = Date.now();
  clearTimeout(typingTimer);
  if (now - lastTypingSent >= gap) {
    lastTypingSent = now;
    hubSend({ op: "typing", text });
  } else {
    typingTimer = setTimeout(() => {
      lastTypingSent = Date.now();
      hubSend({ op: "typing", text: $("guess-input").value });
    }, gap);
  }
}

// best-effort SDK presence: satisfies "see everyone in this activity" and gives us
// a name/avatar fallback. The websocket room stays the authoritative watch list.
async function initPresence(sdk) {
  const apply = (list) => {
    for (const p of list || []) {
      const id = String(p.id);
      if (roomMembers.some((m) => m.id === id)) continue;
      // surface participants who haven't opened their socket yet as "not playing"
      const name = p.global_name || p.nickname || p.username || "Player";
      const avatar = p.avatar
        ? `/api/activity/avatar/${id}?h=${p.avatar}`
        : `/api/activity/avatar/${id}`;
      roomMembers.push({ id, name, avatar, active: false });
    }
    if (!$("spectate-modal").hidden) renderSpectateList();
  };
  const res = await sdk.commands.getInstanceConnectedParticipants();
  apply(res.participants);
  try {
    sdk.subscribe("ACTIVITY_INSTANCE_PARTICIPANTS_UPDATE", (e) => apply(e.participants));
  } catch {}
}

// --- watcher badges (people spectating me) ---

function renderWatchers(list) {
  const box = $("watchers");
  box.innerHTML = "";
  const shown = list.slice(0, 4);
  for (const w of shown) {
    const d = document.createElement("div");
    d.className = "watcher";
    d.title = `${w.name} is watching`;
    d.innerHTML =
      `<img alt="" /><span class="eye"><svg class="icon" viewBox="0 0 24 24"><use href="#i-eye" /></svg></span>`;
    d.querySelector("img").src = `${API}${w.avatar}`;
    box.appendChild(d);
  }
  if (list.length > shown.length) {
    const more = document.createElement("span");
    more.className = "more";
    more.textContent = `+${list.length - shown.length}`;
    box.appendChild(more);
  }
}

// --- spectate picker ---

function openSpectatePicker() {
  renderSpectateList();
  $("spectate-modal").hidden = false;
}
function closeSpectatePicker() {
  $("spectate-modal").hidden = true;
}

function renderSpectateList() {
  const list = $("spectate-list");
  list.innerHTML = "";
  const others = roomMembers
    .filter((m) => m.id !== selfId)
    .sort((a, b) => (b.active ? 1 : 0) - (a.active ? 1 : 0));
  for (const m of others) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.className = m.active ? "spectate-item" : "spectate-item idle";
    // clickable even when idle — you'll watch them and auto-switch to their round
    // the moment they start playing
    btn.innerHTML =
      `<img alt="" /><span class="who"><span class="name"></span><span class="mode"></span></span>`;
    btn.querySelector("img").src = `${API}${m.avatar}`;
    btn.querySelector(".name").textContent = m.name;
    btn.querySelector(".mode").textContent = m.active ? "Playing now" : "Not playing";
    btn.addEventListener("click", () => {
      closeSpectatePicker();
      startSpectating(m.id, m.name);
    });
    li.appendChild(btn);
    list.appendChild(li);
  }
}

// --- spectate view ---

function _spectateBar() {
  return document.querySelector("#screen-spectate .round-bar");
}
function _spectateBody() {
  return document.querySelector("#screen-spectate .round-body");
}

function startSpectating(id, name) {
  spectateTarget = id;
  spectateName = name || "";
  pendingSpectate = null;
  _spectateBar().hidden = false;
  _spectateBody().hidden = false;
  $("spectate-gone").hidden = true;
  resetSpectateView();
  $("spectate-title").textContent = `Watching ${name || "…"}`;
  show("spectate");
  hubSend({ op: "watch", target: id });
}

function stopSpectating() {
  if (spectateTarget) hubSend({ op: "watch", target: null });
  spectateTarget = null;
  pendingSpectate = null;
  stopSpectateTimer();
  show("home");
}

function resetSpectateView() {
  $("spectate-prompt").textContent = "";
  clearRoundMedia("spectate-image", "spectate-video");
  $("spectate-typing").innerHTML = "";
  $("spectate-result").textContent = "";
  $("spectate-result").className = "";
  $("spectate-log").innerHTML = "";
  $("spectate-timer").hidden = true;
  stopSpectateTimer();
}

function applySpectateRound(round) {
  if (!round) return;
  $("spectate-prompt").textContent = round.prompt || "";
  if (round.has_image && round.round_id) {
    setRoundMedia("spectate-image", "spectate-video", round);
  } else {
    clearRoundMedia("spectate-image", "spectate-video");
  }
  if (round.expires_at) startSpectateTimer(round.expires_at);
  else $("spectate-timer").hidden = true;
}

function renderSnapshot(state) {
  resetSpectateView();
  if (!state || !state.round) {
    spectateCleared();
    return;
  }
  applySpectateRound(state.round);
  for (const e of state.log || []) appendLog($("spectate-log"), e.marker, e.text, e.cls);
  spectateTyping(state.typing || "");
  if (state.result) spectateResult(state.result);
}

function spectateRound(round) {
  $("spectate-log").innerHTML = "";
  $("spectate-typing").innerHTML = "";
  $("spectate-result").textContent = "";
  $("spectate-result").className = "";
  applySpectateRound(round);
}

function spectateTyping(text) {
  const el = $("spectate-typing");
  el.innerHTML = "";
  if (text) {
    el.textContent = text;
    const caret = document.createElement("span");
    caret.className = "caret";
    el.appendChild(caret);
  }
}

function spectateResult(result) {
  stopSpectateTimer();
  $("spectate-timer").hidden = true;
  $("spectate-typing").innerHTML = "";
  const el = $("spectate-result");
  el.textContent = result.text || "";
  el.className = result.cls || "";
  if (result.has_reveal && result.round_id) {
    clearVideo($("spectate-video"));
    const img = $("spectate-image");
    img.src = `${API}/api/activity/guess/round/${result.round_id}/reveal`;
    img.hidden = false;
  }
}

function spectateCleared() {
  resetSpectateView();
  $("spectate-prompt").textContent = "They're not in a round right now.";
}

function spectateGone(target) {
  stopSpectateTimer();
  spectateTarget = null;
  pendingSpectate = target || null; // auto-resume when they rejoin the room
  // take over the whole screen so it's unmistakable they left
  _spectateBar().hidden = true;
  _spectateBody().hidden = true;
  $("spectate-gone").hidden = false;
}

function startSpectateTimer(expiresAt) {
  stopSpectateTimer();
  const el = $("spectate-timer");
  el.hidden = false;
  const tick = () => {
    const left = Math.max(0, expiresAt - Date.now() / 1000);
    el.textContent = `${Math.ceil(left)}s`;
    el.classList.toggle("low", left <= 10);
    if (left <= 0) stopSpectateTimer();
  };
  tick();
  spectateTimerHandle = setInterval(tick, 250);
}

function stopSpectateTimer() {
  if (spectateTimerHandle) clearInterval(spectateTimerHandle);
  spectateTimerHandle = null;
}

// --- wiring ---

$("btn-guessing").addEventListener("click", () => show("setup"));
$("topbar-back").addEventListener("click", () => show("home"));
$("btn-settings").addEventListener("click", openSettings);
$("settings-close").addEventListener("click", closeSettings);
$("settings-modal").addEventListener("click", (e) => {
  if (e.target === $("settings-modal")) closeSettings();
});
$("theme-toggle").addEventListener("change", toggleTheme);
$("btn-start").addEventListener("click", () => startRound($("mode-select").value));
$("btn-again").addEventListener("click", () => startRound(currentMode));
$("guess-form").addEventListener("submit", submitGuess);
$("btn-hint").addEventListener("click", useHint);
$("btn-giveup").addEventListener("click", giveUp);

$("btn-quit").addEventListener("click", async () => {
  if (!currentRound) {
    lastRoundPayload = null;
    myGuessLog = [];
    myResult = null;
    hubSend({ op: "clear" }); // already revealed — stop broadcasting the finished round
    return show("setup");
  }
  if (await confirmModal("Quit this round?", "Quit", "Keep playing")) {
    stopTimer();
    currentRound = null;
    lastRoundPayload = null;
    myGuessLog = [];
    myResult = null;
    hubSend({ op: "clear" });
    show("setup");
  }
});

// live typing broadcast + spectate controls
$("guess-input").addEventListener("input", () => throttleTyping($("guess-input").value));
$("btn-spectate").addEventListener("click", openSpectatePicker);
$("spectate-close").addEventListener("click", closeSpectatePicker);
$("spectate-modal").addEventListener("click", (e) => {
  if (e.target === $("spectate-modal")) closeSpectatePicker();
});
$("spectate-stop").addEventListener("click", stopSpectating);
$("spectate-gone-back").addEventListener("click", () => {
  pendingSpectate = null; // they chose to leave, don't auto-resume
  show("home");
});

// version stamp (also proves which app.js is actually running); inline styles so
// it shows even if style.css is stale, and it renders before boot in case boot errors
(function showVersion() {
  const tag = document.createElement("div");
  tag.textContent = `v${APP_VERSION}`;
  tag.style.cssText =
    "position:fixed;right:6px;bottom:4px;z-index:50;font-size:10px;" +
    "font-family:ui-monospace,monospace;color:rgba(150,150,150,0.6);" +
    "pointer-events:none;user-select:none;";
  document.body.appendChild(tag);
})();

// cosmetic — must never block the boot flow
try {
  startRain($("rain"));
  startTrail();
} catch (e) {
  console.error("sbuga fx failed", e);
}

boot().catch((e) => {
  bootError(`Failed to connect: ${e.message}`);
});
