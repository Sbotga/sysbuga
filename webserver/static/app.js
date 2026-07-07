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
let appName = "SYSbuga";

const $ = (id) => document.getElementById(id);

const TITLES = { setup: "Guessing" };

function show(screen) {
  document.querySelectorAll(".screen").forEach((s) => s.classList.remove("active"));
  $(`screen-${screen}`).classList.add("active");

  const bar = $("topbar");
  if (screen === "loading" || screen === "round") {
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

  const { code } = await sdk.commands.authorize({
    client_id: config.client_id,
    response_type: "code",
    state: "",
    scope: ["identify"],
  });

  const token = await api("/api/oauth/token", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
  accessToken = token.access_token;
  await sdk.commands.authenticate({ access_token: accessToken });

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

function logEntry(icon, text, cls) {
  const li = document.createElement("li");
  li.className = cls;
  li.innerHTML = `<span class="marker"></span><span class="text"></span>`;
  li.querySelector(".marker").textContent = icon;
  li.querySelector(".text").textContent = text;
  const log = $("guess-log");
  log.append(li);
  log.scrollTop = log.scrollHeight;
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
  $("btn-giveup").hidden = false;
  $("guess-input").focus();
  startTimer(round.expires_at);
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
    logEntry("💡", `${res.hint}  (${res.length} chars)`, "hint");
  } catch (e) {
    setResult(e.message, "bad");
  }
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
  bootError(`Failed to connect: ${e.message}`);
});
