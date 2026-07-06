import { DiscordSDK } from "./vendor/discord-sdk.js";

// All XHR/fetch inside an activity must go through Discord's proxy prefix.
const API = "/.proxy";

let accessToken = null;
let currentRound = null;
let timerHandle = null;

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

async function boot() {
  const config = await api("/api/config");
  if (config.name) {
    document.title = config.name;
    $("app-name").textContent = config.name;
    document.querySelector("#screen-home h1").textContent = config.name;
  }

  const sdk = new DiscordSDK(config.client_id);
  await sdk.ready();
  const { code } = await sdk.commands.authorize({
    client_id: config.client_id,
    response_type: "code",
    state: "",
    prompt: "none",
    scope: ["identify"],
  });
  const token = await api("/api/oauth/token", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
  accessToken = token.access_token;
  await sdk.commands.authenticate({ access_token: accessToken });

  const modes = await api("/api/activity/modes");
  const select = $("mode-select");
  for (const mode of modes) {
    const option = document.createElement("option");
    option.value = mode.value;
    option.textContent = `${mode.label} (${mode.seconds}s)`;
    select.appendChild(option);
  }

  show("home");
}

function setResult(text, cls) {
  const el = $("round-result");
  el.textContent = text;
  el.className = cls || "";
}

function stopTimer() {
  if (timerHandle) clearInterval(timerHandle);
  timerHandle = null;
}

function startTimer(expiresAt) {
  stopTimer();
  const tick = () => {
    const left = Math.max(0, expiresAt - Date.now() / 1000);
    const el = $("round-timer");
    el.textContent = `${Math.ceil(left)}s`;
    el.classList.toggle("low", left <= 10);
    if (left <= 0) {
      stopTimer();
      endRound(`Time's up!`, "bad");
    }
  };
  tick();
  timerHandle = setInterval(tick, 250);
}

function endRound(message, cls) {
  stopTimer();
  currentRound = null;
  setResult(message, cls);
  $("guess-form").hidden = true;
  $("btn-again").hidden = false;
}

async function startRound() {
  const mode = $("mode-select").value;
  setResult("");
  $("guess-form").hidden = false;
  $("btn-again").hidden = true;
  $("round-image").hidden = true;
  $("round-prompt").textContent = "Loading…";
  show("round");
  $("round-mode").textContent = $("mode-select").selectedOptions[0].textContent;

  try {
    currentRound = await api("/api/activity/guess/start", {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
  } catch (e) {
    $("round-prompt").textContent = `Couldn't start: ${e.message}`;
    $("btn-again").hidden = false;
    $("guess-form").hidden = true;
    return;
  }

  $("round-prompt").textContent = currentRound.prompt || "";
  if (currentRound.has_image) {
    const img = $("round-image");
    img.src = `${API}/api/activity/guess/round/${currentRound.round_id}/image`;
    img.hidden = false;
  }
  $("guess-input").value = "";
  $("guess-input").focus();
  startTimer(currentRound.expires_at);
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
    endRound(`Correct! It was ${result.answer}.`, "good");
  } else if (result.result === "expired") {
    endRound(`Time's up! It was ${result.answer}.`, "bad");
  } else if (result.result === "incorrect") {
    setResult(`Incorrect: ${result.matched}`, "bad");
    $("guess-input").select();
  } else {
    setResult("Couldn't find anything matching that.", "bad");
    $("guess-input").select();
  }
}

$("btn-guessing").addEventListener("click", () => show("setup"));
$("btn-back-home").addEventListener("click", () => show("home"));
$("btn-start").addEventListener("click", startRound);
$("btn-again").addEventListener("click", () => show("setup"));
$("btn-back-setup").addEventListener("click", () => {
  stopTimer();
  currentRound = null;
  show("setup");
});
$("guess-form").addEventListener("submit", submitGuess);

boot().catch((e) => {
  $("loading-text").textContent = `Failed to connect: ${e.message}`;
});
