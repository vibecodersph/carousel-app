(function () {
  const DEFAULT_WIDTH = 1230;
  const DEFAULT_HEIGHT = 1606;
  const DEFAULT_PULL = 49.5;
  const DEFAULT_FONT_SIZE = 118;
  const LEAD_NUDGE_OUT_END = 0.44;
  const LEAD_NUDGE_BACK_END = 0.96;
  const RUBBER_RIGHT_STAGGER = 0.33;
  const RUBBER_RIGHT_DURATION = 1 - RUBBER_RIGHT_STAGGER;
  const RUBBER_PEAK_LOCAL = 0.5;
  const RUBBER_FADE_LOCAL = 0.72;
  const RUBBER_LAST_RETURN_POWER = 1.45;

  const glyphMetrics = {
    A: 0.76,
    B: 0.7,
    C: 0.76,
    D: 0.72,
    E: 0.66,
    F: 0.62,
    G: 0.78,
    H: 0.78,
    I: 0.32,
    J: 0.5,
    K: 0.72,
    L: 0.58,
    M: 0.88,
    N: 0.78,
    O: 0.78,
    P: 0.74,
    Q: 0.78,
    R: 0.72,
    S: 0.68,
    T: 0.66,
    U: 0.76,
    V: 0.78,
    W: 0.95,
    X: 0.72,
    Y: 0.72,
    Z: 0.7,
    a: 0.62,
    b: 0.68,
    c: 0.58,
    d: 0.68,
    e: 0.62,
    f: 0.5,
    g: 0.66,
    h: 0.68,
    i: 0.32,
    j: 0.42,
    k: 0.68,
    l: 0.34,
    m: 0.98,
    n: 0.66,
    o: 0.64,
    p: 0.66,
    q: 0.66,
    r: 0.48,
    s: 0.58,
    t: 0.48,
    u: 0.66,
    v: 0.62,
    w: 0.88,
    x: 0.62,
    y: 0.66,
    z: 0.58
  };

  const defaultStyles = [
    { name: "lead-gather", y: 220 },
    { name: "anchored-lead", y: 720 },
    { name: "rubber-lead", y: 1220 }
  ];

  const clamp = (value, min = 0, max = 1) => Math.min(max, Math.max(min, value));

  const smoothstep = (edge0, edge1, value) => {
    const t = clamp((value - edge0) / (edge1 - edge0));
    return t * t * (3 - 2 * t);
  };

  const metricFor = (char) => glyphMetrics[char] || 0.68;

  const normalizeWords = (words) => words
    .map((word) => {
      const text = typeof word === "string" ? word : word && word.text;
      const metrics = word && typeof word === "object" ? word.metrics : null;
      return {
        text: String(text || ""),
        metrics: metrics || Array.from(String(text || "")).map(metricFor),
        reverse: Boolean(word && typeof word === "object" && (word.reverse || word.highlighted))
      };
    })
    .filter((word) => word.text);

  const createLayer = (className, char) => {
    const layer = document.createElement("span");
    layer.className = `tm-layer ${className}`;
    layer.textContent = char;
    return layer;
  };

  const styleOpacity = (styleName, opacity) => styleName === "s1"
    ? [opacity, 0]
    : [0, opacity];

  class TextMotionBoard {
    constructor(options = {}) {
      this.options = {
        width: DEFAULT_WIDTH,
        height: DEFAULT_HEIGHT,
        activeMs: 1500,
        outActiveMs: null,
        returnActiveMs: null,
        pauseMs: 2000,
        pull: DEFAULT_PULL,
        fontSize: DEFAULT_FONT_SIZE,
        fontFamily: "",
        fontWeight: 780,
        rowLeft: 92,
        rowWidth: 1048,
        rowHeight: 250,
        fitTo: "window",
        autoStart: true,
        showLabels: true,
        viewportClassName: "tm-viewport",
        stageClassName: "tm-stage",
        styles: defaultStyles,
        words: [
          { text: "Vibe" },
          { text: "CodersPH" }
        ],
        ...options
      };

      this.mount = typeof this.options.mount === "string"
        ? document.querySelector(this.options.mount)
        : this.options.mount;

      if (!this.mount) {
        throw new Error("TextMotionBoard requires a mount element.");
      }

      this.words = normalizeWords(this.options.words);
      this.rows = [];
      this.frame = null;
      this.resizeObserver = null;
      this.startTime = null;
      this.build();
      this.resize();
      window.addEventListener("resize", () => this.resize(), { passive: true });
      if (this.options.fitTo === "mount" && typeof ResizeObserver !== "undefined") {
        this.resizeObserver = new ResizeObserver(() => this.resize());
        this.resizeObserver.observe(this.mount);
      }
      if (this.options.autoStart === false) {
        this.setTime(this.options.startMs || 0);
      } else {
        this.start();
      }
    }

    build() {
      this.mount.textContent = "";

      this.viewport = document.createElement("main");
      this.stage = document.createElement("section");
      this.viewport.className = this.options.viewportClassName;
      this.viewport.setAttribute("aria-label", this.options.ariaLabel || "Animated typography styles");
      this.stage.className = this.options.stageClassName;
      this.stage.style.setProperty("--tm-width", this.options.width);
      this.stage.style.setProperty("--tm-height", this.options.height);
      this.stage.style.setProperty("--tm-row-left", `${this.options.rowLeft}px`);
      this.stage.style.setProperty("--tm-row-width", `${this.options.rowWidth}px`);
      this.stage.style.setProperty("--tm-row-height", `${this.options.rowHeight}px`);
      this.stage.style.setProperty("--tm-font-size", `${this.options.fontSize}px`);
      if (this.options.fontFamily) {
        this.stage.style.setProperty("--tm-font-family", this.options.fontFamily);
      }
      this.stage.style.setProperty("--tm-font-weight", `${this.options.fontWeight}`);
      this.viewport.appendChild(this.stage);
      this.mount.appendChild(this.viewport);

      this.rows = this.options.styles.map((style) => this.createRow(style));
    }

    createRow(style) {
      const row = document.createElement("div");
      const label = document.createElement("div");
      const phrase = document.createElement("div");
      const letters = [];
      const lineLetterCount = this.words.reduce((count, word) => count + word.text.length, 0);
      const lineLead = -Math.round(this.words[0].metrics[0] * this.options.fontSize);
      let lineIndex = 0;

      row.className = "tm-row";
      row.style.top = `${style.y}px`;
      row.dataset.motion = style.name;

      label.className = "tm-label";
      label.textContent = style.label || style.name;

      phrase.className = "tm-phrase";

      this.words.forEach((word) => {
        const wordNode = document.createElement("div");
        wordNode.className = word.reverse ? "tm-word tm-word--reverse" : "tm-word";

        Array.from(word.text).forEach((char, index) => {
          const letter = document.createElement("span");
          const s1 = createLayer("tm-style-one", char);
          const s2 = createLayer("tm-style-two", char);

          letter.className = word.reverse ? "tm-letter tm-letter--reverse" : "tm-letter";
          letter.style.setProperty("--tm-char-width", `${word.metrics[index]}em`);
          letter.style.setProperty("--tm-char-height", "0.86em");
          letter.append(s1, s2);
          wordNode.appendChild(letter);

          letters.push({
            index: lineIndex,
            count: lineLetterCount,
            rightX: this.options.pull,
            rightY: 0,
            pullX: this.options.pull,
            pullY: 0,
            leadX: lineLead,
            leadY: 0,
            reverse: word.reverse,
            el: letter,
            s1,
            s2
          });
          lineIndex += 1;
        });

        phrase.appendChild(wordNode);
      });

      if (this.options.showLabels !== false) {
        row.appendChild(label);
      }
      row.appendChild(phrase);
      this.stage.appendChild(row);
      return { name: style.name, letters };
    }

    resize() {
      const fitWidth = this.options.fitTo === "mount"
        ? (this.mount.clientWidth || this.options.width)
        : window.innerWidth;
      const fitHeight = this.options.fitTo === "mount"
        ? (this.mount.clientHeight || this.options.height)
        : window.innerHeight;
      const scale = this.options.fitTo === "none"
        ? 1
        : Math.min(fitWidth / this.options.width, fitHeight / this.options.height);
      this.stage.style.setProperty("--tm-scale", scale.toString());
    }

    start() {
      this.stop();
      const tick = (time) => {
        if (this.startTime == null) this.startTime = time;
        this.render(time - this.startTime);
        this.frame = requestAnimationFrame(tick);
      };
      this.frame = requestAnimationFrame(tick);
    }

    stop() {
      if (this.frame != null) {
        cancelAnimationFrame(this.frame);
        this.frame = null;
      }
    }

    setTime(timeMs) {
      this.render(Math.max(0, Number(timeMs) || 0));
    }

    loopDurationMs() {
      const timing = this.motionTiming();
      return timing.outActiveMs + timing.returnActiveMs + (this.options.pauseMs * 2);
    }

    motionTiming() {
      const returnActiveMs = Math.max(
        1,
        this.options.returnActiveMs || (this.options.activeMs / 2)
      );
      const outActiveMs = Math.max(
        1,
        this.options.outActiveMs || (returnActiveMs * 1.2)
      );
      return { outActiveMs, returnActiveMs };
    }

    setProgress(progress, delayMs = 0) {
      const loopMs = this.loopDurationMs();
      const rawTime = (clamp(progress) * loopMs) - (Number(delayMs) || 0);
      const loopedTime = ((rawTime % loopMs) + loopMs) % loopMs;
      this.setTime(loopedTime);
    }

    render(time) {
      const { outActiveMs, returnActiveMs } = this.motionTiming();
      const totalMs = this.loopDurationMs();
      const elapsed = time % totalMs;

      if (elapsed < outActiveMs) {
        const progress = elapsed / outActiveMs;
        this.rows.forEach((row) => this.renderHalfCycle(row, progress, "s1", "s2"));
        return;
      }

      if (elapsed < outActiveMs + this.options.pauseMs) {
        this.rows.forEach((row) => this.renderRest(row, "s2"));
        return;
      }

      if (elapsed < outActiveMs + this.options.pauseMs + returnActiveMs) {
        const progress = (elapsed - outActiveMs - this.options.pauseMs) / returnActiveMs;
        this.rows.forEach((row) => this.renderHalfCycle(row, progress, "s2", "s1"));
        return;
      }

      this.rows.forEach((row) => this.renderRest(row, "s1"));
    }

    setLetter(letter, s1Opacity, s2Opacity, x = 0, y = 0) {
      if (letter.reverse) {
        [s1Opacity, s2Opacity] = [s2Opacity, s1Opacity];
      }
      letter.el.style.opacity = "1";
      letter.el.style.transform = `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px)`;
      letter.s1.style.opacity = s1Opacity.toFixed(3);
      letter.s2.style.opacity = s2Opacity.toFixed(3);
    }

    leadNudge(letter, progress) {
      const nudgeOut = smoothstep(0, LEAD_NUDGE_OUT_END, progress);
      const nudgeBack = smoothstep(LEAD_NUDGE_OUT_END, LEAD_NUDGE_BACK_END, progress);
      const nudge = nudgeOut * (1 - nudgeBack);
      return [letter.leadX * nudge, letter.leadY * nudge];
    }

    rightRubberProgressAt(letter, local) {
      const step = letter.index / Math.max(1, letter.count - 1);
      return (step * RUBBER_RIGHT_STAGGER) + (local * RUBBER_RIGHT_DURATION);
    }

    rightRubberMotion(letter, progress, options = {}) {
      const step = letter.index / Math.max(1, letter.count - 1);
      let local = clamp((progress - step * RUBBER_RIGHT_STAGGER) / RUBBER_RIGHT_DURATION);

      if (letter.index === letter.count - 1 && local > RUBBER_PEAK_LOCAL) {
        const returnT = (local - RUBBER_PEAK_LOCAL) / (1 - RUBBER_PEAK_LOCAL);
        local = RUBBER_PEAK_LOCAL
          + (Math.pow(returnT, RUBBER_LAST_RETURN_POWER) * (1 - RUBBER_PEAK_LOCAL));
      }

      const stretch = Math.sin(local * Math.PI);
      const boost = options.leadBoost && letter.index === 0 ? 1.18 : 1;
      const factor = Math.pow(stretch, 0.72) * boost;

      return {
        local,
        x: letter.rightX * factor,
        y: letter.rightY * factor
      };
    }

    renderRest(row, styleName) {
      const [s1Opacity, s2Opacity] = styleOpacity(styleName, 1);
      row.letters.forEach((letter) => this.setLetter(letter, s1Opacity, s2Opacity, 0, 0));
    }

    renderRubberHalfCycle(row, progress, fromStyle, toStyle) {
      row.letters.forEach((letter) => {
        const motion = this.rightRubberMotion(letter, progress, { leadBoost: true });
        const styleMix = smoothstep(0.42, 0.58, motion.local);
        const s1Opacity = fromStyle === "s1" ? 1 - styleMix : styleMix;
        const s2Opacity = toStyle === "s2" ? styleMix : 1 - styleMix;

        this.setLetter(letter, s1Opacity, s2Opacity, motion.x, motion.y);
      });
    }

    renderAnchoredHalfCycle(row, progress, fromStyle, toStyle) {
      const lastLetter = row.letters[row.letters.length - 1];
      const leadSwitchAt = lastLetter
        ? this.rightRubberProgressAt(lastLetter, RUBBER_PEAK_LOCAL)
        : RUBBER_PEAK_LOCAL;

      row.letters.forEach((letter) => {
        if (letter.index === 0) {
          const styleName = progress < leadSwitchAt ? fromStyle : toStyle;
          const [s1Opacity, s2Opacity] = styleOpacity(styleName, 1);
          this.setLetter(letter, s1Opacity, s2Opacity, 0, 0);
          return;
        }

        const motion = this.rightRubberMotion(letter, progress);
        const vanishStart = this.rightRubberProgressAt(letter, RUBBER_FADE_LOCAL);
        const fadeOutEnd = vanishStart + 0.055;
        const appearStart = fadeOutEnd + 0.035;
        const fadeInEnd = appearStart + 0.075;

        if (progress < vanishStart) {
          const [s1Opacity, s2Opacity] = styleOpacity(fromStyle, 1);
          this.setLetter(letter, s1Opacity, s2Opacity, motion.x, motion.y);
        } else if (progress < fadeOutEnd) {
          const opacity = 1 - (progress - vanishStart) / (fadeOutEnd - vanishStart);
          const [s1Opacity, s2Opacity] = styleOpacity(fromStyle, opacity);
          this.setLetter(letter, s1Opacity, s2Opacity, motion.x, motion.y);
        } else if (progress < appearStart) {
          this.setLetter(letter, 0, 0, 0, 0);
        } else if (progress < fadeInEnd) {
          const opacity = (progress - appearStart) / (fadeInEnd - appearStart);
          const [s1Opacity, s2Opacity] = styleOpacity(toStyle, opacity);
          this.setLetter(letter, s1Opacity, s2Opacity, 0, 0);
        } else {
          const [s1Opacity, s2Opacity] = styleOpacity(toStyle, 1);
          this.setLetter(letter, s1Opacity, s2Opacity, 0, 0);
        }
      });
    }

    renderLeadGatherHalfCycle(row, progress, fromStyle, toStyle) {
      row.letters.forEach((letter) => {
        if (letter.index === 0) {
          const [x, y] = this.leadNudge(letter, progress);
          const styleName = progress < LEAD_NUDGE_OUT_END ? fromStyle : toStyle;
          const [s1Opacity, s2Opacity] = styleOpacity(styleName, 1);
          this.setLetter(letter, s1Opacity, s2Opacity, x, y);
          return;
        }

        const motion = this.rightRubberMotion(letter, progress);
        const vanishStart = this.rightRubberProgressAt(letter, RUBBER_FADE_LOCAL);
        const fadeOutEnd = vanishStart + 0.055;
        const appearStart = fadeOutEnd + 0.035;
        const fadeInEnd = appearStart + 0.075;
        const [gatherX, gatherY] = this.leadNudge(letter, progress);

        if (progress < vanishStart) {
          const [s1Opacity, s2Opacity] = styleOpacity(fromStyle, 1);
          this.setLetter(letter, s1Opacity, s2Opacity, motion.x, motion.y);
        } else if (progress < fadeOutEnd) {
          const opacity = 1 - (progress - vanishStart) / (fadeOutEnd - vanishStart);
          const [s1Opacity, s2Opacity] = styleOpacity(fromStyle, opacity);
          this.setLetter(letter, s1Opacity, s2Opacity, motion.x, motion.y);
        } else if (progress < appearStart) {
          this.setLetter(letter, 0, 0, gatherX, gatherY);
        } else if (progress < fadeInEnd) {
          const opacity = (progress - appearStart) / (fadeInEnd - appearStart);
          const [s1Opacity, s2Opacity] = styleOpacity(toStyle, opacity);
          this.setLetter(letter, s1Opacity, s2Opacity, gatherX, gatherY);
        } else {
          const [s1Opacity, s2Opacity] = styleOpacity(toStyle, 1);
          this.setLetter(letter, s1Opacity, s2Opacity, gatherX, gatherY);
        }
      });
    }

    renderHalfCycle(row, progress, fromStyle, toStyle) {
      if (row.name === "rubber-lead") {
        this.renderRubberHalfCycle(row, progress, fromStyle, toStyle);
      } else if (row.name === "anchored-lead") {
        this.renderAnchoredHalfCycle(row, progress, fromStyle, toStyle);
      } else {
        this.renderLeadGatherHalfCycle(row, progress, fromStyle, toStyle);
      }
    }
  }

  window.TextMotion = {
    TextMotionBoard,
    createBoard: (options) => new TextMotionBoard(options),
    defaultStyles,
    defaultPull: DEFAULT_PULL,
    glyphMetrics,
    metricFor
  };
}());
