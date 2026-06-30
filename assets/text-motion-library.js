(function () {
  const DEFAULT_WIDTH = 1230;
  const DEFAULT_HEIGHT = 1606;
  const DEFAULT_PULL = 168;
  const DEFAULT_FONT_SIZE = 118;

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

  const normalizeWords = (words) => words.map((word) => ({
    text: word.text,
    metrics: word.metrics || Array.from(word.text).map(metricFor)
  }));

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
        pauseMs: 2000,
        pull: DEFAULT_PULL,
        fontSize: DEFAULT_FONT_SIZE,
        rowLeft: 92,
        rowWidth: 1048,
        rowHeight: 250,
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
      this.startTime = null;
      this.build();
      this.resize();
      window.addEventListener("resize", () => this.resize(), { passive: true });
      this.start();
    }

    build() {
      this.mount.textContent = "";

      this.viewport = document.createElement("main");
      this.stage = document.createElement("section");
      this.viewport.className = "tm-viewport";
      this.viewport.setAttribute("aria-label", this.options.ariaLabel || "Animated typography styles");
      this.stage.className = "tm-stage";
      this.stage.style.setProperty("--tm-width", this.options.width);
      this.stage.style.setProperty("--tm-height", this.options.height);
      this.stage.style.setProperty("--tm-row-left", `${this.options.rowLeft}px`);
      this.stage.style.setProperty("--tm-row-width", `${this.options.rowWidth}px`);
      this.stage.style.setProperty("--tm-row-height", `${this.options.rowHeight}px`);
      this.stage.style.setProperty("--tm-font-size", `${this.options.fontSize}px`);
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
        wordNode.className = "tm-word";

        Array.from(word.text).forEach((char, index) => {
          const letter = document.createElement("span");
          const s1 = createLayer("tm-style-one", char);
          const s2 = createLayer("tm-style-two", char);

          letter.className = "tm-letter";
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
            el: letter,
            s1,
            s2
          });
          lineIndex += 1;
        });

        phrase.appendChild(wordNode);
      });

      row.append(label, phrase);
      this.stage.appendChild(row);
      return { name: style.name, letters };
    }

    resize() {
      const scale = Math.min(
        window.innerWidth / this.options.width,
        window.innerHeight / this.options.height
      );
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

    render(time) {
      const totalMs = this.options.activeMs + this.options.pauseMs;
      const elapsed = time % totalMs;

      if (elapsed >= this.options.activeMs) {
        this.rows.forEach((row) => this.renderRest(row));
        return;
      }

      const cycle = elapsed / this.options.activeMs;
      this.rows.forEach((row) => {
        if (cycle < 0.5) {
          this.renderHalfCycle(row, cycle * 2, "s1", "s2");
        } else {
          this.renderHalfCycle(row, (cycle - 0.5) * 2, "s2", "s1");
        }
      });
    }

    setLetter(letter, s1Opacity, s2Opacity, x = 0, y = 0) {
      letter.el.style.opacity = "1";
      letter.el.style.transform = `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px)`;
      letter.s1.style.opacity = s1Opacity.toFixed(3);
      letter.s2.style.opacity = s2Opacity.toFixed(3);
    }

    leadNudge(letter, progress) {
      const nudgeOut = smoothstep(0, 0.44, progress);
      const nudgeBack = smoothstep(0.44, 0.96, progress);
      const nudge = nudgeOut * (1 - nudgeBack);
      return [letter.leadX * nudge, letter.leadY * nudge];
    }

    renderRest(row) {
      row.letters.forEach((letter) => this.setLetter(letter, 1, 0, 0, 0));
    }

    renderRubberHalfCycle(row, progress, fromStyle, toStyle) {
      row.letters.forEach((letter) => {
        const step = letter.index / Math.max(1, letter.count - 1);
        const leadBoost = letter.index === 0 ? 1.18 : 1;
        const local = clamp((progress - step * 0.33) / 0.67);
        const stretch = Math.sin(local * Math.PI);
        const rubber = Math.pow(stretch, 0.72) * leadBoost;
        const styleMix = smoothstep(0.42, 0.58, local);
        const s1Opacity = fromStyle === "s1" ? 1 - styleMix : styleMix;
        const s2Opacity = toStyle === "s2" ? styleMix : 1 - styleMix;

        this.setLetter(letter, s1Opacity, s2Opacity, letter.pullX * rubber, letter.pullY * rubber);
      });
    }

    renderAnchoredHalfCycle(row, progress, fromStyle, toStyle) {
      row.letters.forEach((letter) => {
        if (letter.index === 0) {
          const styleName = progress < 0.14 ? fromStyle : toStyle;
          const [s1Opacity, s2Opacity] = styleOpacity(styleName, 1);
          this.setLetter(letter, s1Opacity, s2Opacity, 0, 0);
          return;
        }

        const step = (letter.index - 1) / Math.max(1, letter.count - 2);
        const moveStart = 0.10 + step * 0.08;
        const moveEnd = moveStart + 0.18;
        const vanishStart = 0.30 + step * 0.30;
        const fadeOutEnd = vanishStart + 0.055;
        const appearStart = fadeOutEnd + 0.035;
        const fadeInEnd = appearStart + 0.075;
        const shift = smoothstep(moveStart, moveEnd, progress);

        if (progress < vanishStart) {
          const [s1Opacity, s2Opacity] = styleOpacity(fromStyle, 1);
          this.setLetter(letter, s1Opacity, s2Opacity, letter.rightX * shift, letter.rightY * shift);
        } else if (progress < fadeOutEnd) {
          const opacity = 1 - (progress - vanishStart) / (fadeOutEnd - vanishStart);
          const [s1Opacity, s2Opacity] = styleOpacity(fromStyle, opacity);
          this.setLetter(letter, s1Opacity, s2Opacity, letter.rightX, letter.rightY);
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
          const styleName = progress < 0.44 ? fromStyle : toStyle;
          const [s1Opacity, s2Opacity] = styleOpacity(styleName, 1);
          this.setLetter(letter, s1Opacity, s2Opacity, x, y);
          return;
        }

        const step = (letter.index - 1) / Math.max(1, letter.count - 2);
        const moveStart = 0.10 + step * 0.08;
        const moveEnd = moveStart + 0.18;
        const vanishStart = 0.30 + step * 0.30;
        const fadeOutEnd = vanishStart + 0.055;
        const appearStart = fadeOutEnd + 0.035;
        const fadeInEnd = appearStart + 0.075;
        const shift = smoothstep(moveStart, moveEnd, progress);
        const [gatherX, gatherY] = this.leadNudge(letter, progress);

        if (progress < vanishStart) {
          const [s1Opacity, s2Opacity] = styleOpacity(fromStyle, 1);
          this.setLetter(letter, s1Opacity, s2Opacity, letter.rightX * shift, letter.rightY * shift);
        } else if (progress < fadeOutEnd) {
          const opacity = 1 - (progress - vanishStart) / (fadeOutEnd - vanishStart);
          const [s1Opacity, s2Opacity] = styleOpacity(fromStyle, opacity);
          this.setLetter(letter, s1Opacity, s2Opacity, letter.rightX, letter.rightY);
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
    glyphMetrics,
    metricFor
  };
}());
