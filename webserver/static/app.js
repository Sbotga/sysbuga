import { DiscordSDK } from "./vendor/discord-sdk.js";
import { startRain, startTrail } from "./sbuga.js";

// Inside the activity iframe all XHR/fetch goes through Discord's proxy
// prefix; hit directly (testing), the server has no such prefix.
const EMBEDDED =
  location.hostname.endsWith("discordsays.com") ||
  new URLSearchParams(location.search).has("frame_id");
const API = EMBEDDED ? "/.proxy" : "";

let accessToken = null;
let currentMode = null;
let currentRound = null; // active (unfinished) round, else null
let timerHandle = null;
let theme = "dark";

const $ = (id) => document.getElementById(id);

function show(screen) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.remove("active"));
  $(`screen-${screen}`).classList.add("active");
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
  $("btn-theme").textContent = theme === "light" ? "☀️" : "🌙";
}

async function toggleTheme() {
  theme = theme === "light" ? "dark" : "light";
  applyTheme();
  try {
    await api("/api/activity/settings", {
      method: "POST",
      body: JSON.stringify({ theme }),
    });
  } catch {}
}

// --- boot / auth ---

const status = (t) => ($("loading-text").textContent = t);

async function boot() {
  status("Contacting server…");
  const config = await api("/api/config");
  if (config.name) {
    document.title = config.name;
    $("app-name").textContent = config.name;
    document.querySelector("#screen-home h1").textContent = config.name;
  }

  if (!EMBEDDED) {
    status("Server is up! Open this as an activity inside Discord to play.");
    return;
  }
  if (!config.client_id) {
    status("Server isn't configured yet (no client id).");
    return;
  }

  status("Handshaking with Discord…");
  const sdk = new DiscordSDK(config.client_id);
  await sdk.ready();

  status("Authorizing…");
  const { code } = await sdk.commands.authorize({
    client_id: config.client_id,
    response_type: "code",
    state: "",
    scope: ["identify"],
  });

  status("Signing in…");
  const token = await api("/api/oauth/token", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
  accessToken = token.access_token;
  await sdk.commands.authenticate({ access_token: accessToken });

  status("Loading…");
  try {
    const settings = await api("/api/activity/settings");
    theme = settings.theme === "light" ? "light" : "dark";
  } catch {}
  applyTheme();
  $("btn-theme").hidden = false;

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

function logEntry(icon, text, cls) {
  const li = document.createElement("li");
  li.className = cls;
  li.innerHTML = `<span class="icon"></span><span class="text"></span>`;
  li.querySelector(".icon").textContent = icon;
  li.querySelector(".text").textContent = text;
  const log = $("guess-log");
  log.prepend(li);
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
  $("round-image").hidden = true;
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
    return;
  }

  currentRound = round;
  $("round-prompt").textContent = round.prompt || "";
  if (round.has_image) {
    const img = $("round-image");
    img.src = `${API}/api/activity/guess/round/${round.round_id}/image`;
    img.hidden = false;
  }
  setFormEnabled(true);
  $("guess-input").focus();
  startTimer(round.expires_at);
}

function showReveal(round, message, cls) {
  stopTimer();
  currentRound = null;
  $("round-timer").hidden = true;
  setResult(message, cls);
  $("guess-form").hidden = true;
  $("btn-hint").disabled = true;
  $("btn-again").hidden = false;
  if (round.has_reveal) {
    const img = $("round-image");
    img.src = `${API}/api/activity/guess/round/${round.round_id}/reveal`;
    img.hidden = false;
  }
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
    logEntry("✅", guess, "right");
    showReveal({ ...currentRound, ...result }, `Correct! It was ${result.answer}.`, "good");
  } else if (result.result === "expired") {
    showReveal({ ...currentRound, ...result }, `Time's up! It was ${result.answer}.`, "bad");
  } else if (result.result === "incorrect") {
    logEntry("❌", result.matched, "wrong");
    $("guess-input").value = "";
    $("guess-input").focus();
  } else {
    logEntry("❔", guess, "miss");
    $("guess-input").select();
  }
}

async function timeUp() {
  if (!currentRound) return;
  const round = currentRound;
  try {
    const res = await api("/api/activity/guess/reveal", {
      method: "POST",
      body: JSON.stringify({ round_id: round.round_id }),
    });
    showReveal({ ...round, ...res }, `Time's up! It was ${res.answer}.`, "bad");
  } catch {
    showReveal(round, "Time's up!", "bad");
  }
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
    logEntry("💡", `${res.hint}  (${res.length} chars)`, "hint");
  } catch (e) {
    setResult(e.message, "bad");
  }
}

// --- wiring ---

$("btn-guessing").addEventListener("click", () => show("setup"));
document
  .querySelectorAll(".back[data-target]")
  .forEach((b) => b.addEventListener("click", () => show(b.dataset.target)));
$("btn-start").addEventListener("click", () => startRound($("mode-select").value));
$("btn-again").addEventListener("click", () => startRound(currentMode));
$("guess-form").addEventListener("submit", submitGuess);
$("btn-hint").addEventListener("click", useHint);
$("btn-theme").addEventListener("click", toggleTheme);

$("btn-quit").addEventListener("click", async () => {
  if (!currentRound) return show("setup"); // already revealed
  if (await confirmModal("Quit this round?", "Quit", "Keep playing")) {
    stopTimer();
    currentRound = null;
    show("setup");
  }
});

// cosmetic — must never block the boot flow
try {
  startRain($("rain"));
  startTrail();
} catch (e) {
  console.error("sbuga fx failed", e);
}

boot().catch((e) => {
  status(`Failed to connect: ${e.message}`);
});
