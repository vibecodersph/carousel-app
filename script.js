const frame = document.querySelector(".cover-frame");

let targetX = 0;
let targetY = 0;
let currentX = 0;
let currentY = 0;

function setTargetFromPoint(clientX, clientY) {
  const rect = frame.getBoundingClientRect();
  const x = (clientX - rect.left) / rect.width - 0.5;
  const y = (clientY - rect.top) / rect.height - 0.5;

  targetX = Math.max(-1, Math.min(1, x * 2));
  targetY = Math.max(-1, Math.min(1, y * 2));
}

function resetTarget() {
  targetX = 0;
  targetY = 0;
}

function tick() {
  currentX += (targetX - currentX) * 0.08;
  currentY += (targetY - currentY) * 0.08;

  frame.style.setProperty("--mx", currentX.toFixed(3));
  frame.style.setProperty("--my", currentY.toFixed(3));

  requestAnimationFrame(tick);
}

if (frame) {
  frame.addEventListener("pointermove", (event) => {
    setTargetFromPoint(event.clientX, event.clientY);
  });

  frame.addEventListener("pointerleave", resetTarget);
  frame.addEventListener("pointercancel", resetTarget);

  tick();
}
