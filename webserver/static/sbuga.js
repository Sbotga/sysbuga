// Background rain of sbuga + a trail of sbuga following the pointer.
// Ported from sbuga.com's rain.js / trail.js, canvas-based for mobile perf.

const RAIN_IMAGES = [
  "sbuga.png", "sbuga_cute.png", "sbuga_red.png", "sbuga_green.png",
  "sbuga_orange.png", "sbuga_purple.png", "sbuga_yellow.png", "zuba.png",
];
const TRAIL_IMAGES = [...RAIN_IMAGES, "sbuga_spin.gif", "sbuga_pat.gif"];

function load(name) {
  const img = new Image();
  img.src = `sbuga/${name}`;
  return img;
}

const rainImgs = RAIN_IMAGES.map(load);
const trailSrcs = TRAIL_IMAGES.map((n) => `sbuga/${n}`);

// --- canvas rain ---

export function startRain(canvas) {
  const ctx = canvas.getContext("2d");
  let drops = [];
  let width = 0;
  let height = 0;

  function resize() {
    width = canvas.width = window.innerWidth;
    height = canvas.height = window.innerHeight;
    const target = Math.round((width * height) / 26000); // density
    while (drops.length < target) drops.push(newDrop(true));
    drops.length = target;
  }

  function newDrop(anywhere) {
    return {
      img: rainImgs[(Math.random() * rainImgs.length) | 0],
      x: Math.random() * width,
      y: anywhere ? Math.random() * height : -30,
      size: 18 + Math.random() * 16,
      speed: 0.6 + Math.random() * 1.6,
      drift: (Math.random() - 0.5) * 0.4,
    };
  }

  function frame() {
    ctx.clearRect(0, 0, width, height);
    ctx.globalAlpha = 0.55;
    for (const d of drops) {
      d.y += d.speed;
      d.x += d.drift;
      if (d.y > height + 30) Object.assign(d, newDrop(false));
      if (d.img.complete && d.img.naturalWidth) {
        ctx.drawImage(d.img, d.x, d.y, d.size, d.size);
      }
    }
    ctx.globalAlpha = 1;
    requestAnimationFrame(frame);
  }

  window.addEventListener("resize", resize);
  resize();
  requestAnimationFrame(frame);
}

// --- pointer trail ---

export function startTrail() {
  let last = 0;
  const spawn = (x, y) => {
    if (performance.now() - last < 40) return;
    last = performance.now();
    const el = document.createElement("img");
    el.className = "sbuga-trail";
    el.src = trailSrcs[(Math.random() * trailSrcs.length) | 0];
    el.style.left = `${x - 12}px`;
    el.style.top = `${y - 12}px`;
    document.body.appendChild(el);
    requestAnimationFrame(() => el.classList.add("fade"));
    setTimeout(() => el.remove(), 400);
  };
  window.addEventListener("mousemove", (e) => spawn(e.clientX, e.clientY));
  window.addEventListener(
    "touchmove",
    (e) => {
      const t = e.touches[0];
      if (t) spawn(t.clientX, t.clientY);
    },
    { passive: true }
  );
}
