/* Camera barcode scanner for book barcodes (EAN-13 / ISBN, UPC).
 *
 * Why not just a library's default setup:
 *  - iPhone Safari has no built-in BarcodeDetector, so we use ZXing C++ compiled to
 *    WebAssembly (via the barcode-detector ponyfill). It is far better at 1D barcodes
 *    than the older pure-JavaScript decoders. Browsers with a working native detector
 *    (Chrome on Android) use that instead.
 *  - Book barcodes have thin bars, so we ask for a high-resolution stream and decode
 *    the full-resolution pixels inside the on-screen box rather than a shrunken frame.
 *  - Recent Pro iPhones can't focus closer than about 15 cm with the main lens. When
 *    Safari lists Apple's multi-lens "virtual" cameras we prefer them, since they can
 *    hand off to the close-focus lens. Zoom (where supported) lets you hold the phone
 *    farther back instead. A photo fallback uses the system camera, which always focuses.
 */
(function () {
  "use strict";

  const FORMATS = ["ean_13", "ean_8", "upc_a", "upc_e"];
  const CAMERA_KEY = "hl.cameraId";
  const PREFERRED_LABELS = [/triple camera/i, /dual wide camera/i, /dual camera/i];

  let detectorPromise = null;

  function wasmUrl() {
    return new URL("static/vendor/zxing_reader.wasm", document.baseURI).href;
  }

  async function nativeDetector() {
    if (!("BarcodeDetector" in window)) return null;
    try {
      const supported = await window.BarcodeDetector.getSupportedFormats();
      if (!supported.includes("ean_13")) return null;
      return new window.BarcodeDetector({ formats: FORMATS.filter((f) => supported.includes(f)) });
    } catch {
      return null;
    }
  }

  function getDetector() {
    if (!detectorPromise) {
      detectorPromise = (async () => {
        const native = await nativeDetector();
        if (native) return { detector: native, engine: "native" };
        const api = window.BarcodeDetectionAPI;
        if (!api) throw new Error("The barcode reader didn't load. Reload the page and try again.");
        api.prepareZXingModule({
          overrides: { locateFile: (path, prefix) => (path.endsWith(".wasm") ? wasmUrl() : prefix + path) },
          fireImmediately: true,
        });
        return { detector: new api.BarcodeDetector({ formats: FORMATS }), engine: "zxing" };
      })();
      detectorPromise.catch(() => { detectorPromise = null; });
    }
    return detectorPromise;
  }

  function plausible(code) {
    const c = String(code || "").replace(/\D/g, "");
    return c.length === 13 || c.length === 12 || c.length === 8;
  }

  async function detectOn(source) {
    const { detector } = await getDetector();
    const found = await detector.detect(source);
    const hit = found.map((b) => b.rawValue).find(plausible);
    return hit || null;
  }

  // Decode a still photo (the system camera app focuses up close reliably).
  async function decodeImageFile(file) {
    const bmp = await createImageBitmap(file);
    try {
      const max = 2400;
      const scale = Math.min(1, max / Math.max(bmp.width, bmp.height));
      const c = document.createElement("canvas");
      c.width = Math.round(bmp.width * scale);
      c.height = Math.round(bmp.height * scale);
      c.getContext("2d", { willReadFrequently: true }).drawImage(bmp, 0, 0, c.width, c.height);
      return await detectOn(c);
    } finally {
      bmp.close?.();
    }
  }

  function isBackCamera(d) {
    return !/front|facetime|user|selfie/i.test(d.label || "");
  }

  class BookScanner {
    constructor(video, { box, onDetect, onError } = {}) {
      this.video = video;
      this.box = box;               // element marking the scan area on screen
      this.onDetect = onDetect;
      this.onError = onError;
      this.stream = null;
      this.track = null;
      this.running = false;
      this.paused = false;
      this.cameras = [];
      this.engine = "";
      this.canvas = document.createElement("canvas");
      this.ctx = this.canvas.getContext("2d", { willReadFrequently: true });
      this.tick = 0;
      this.recent = { code: "", at: 0 };
    }

    static decodeImageFile(file) { return decodeImageFile(file); }
    static preload() { return getDetector(); }

    async start(deviceId = null) {
      const { engine } = await getDetector();
      this.engine = engine;
      let saved = deviceId;
      if (!saved) { try { saved = localStorage.getItem(CAMERA_KEY); } catch { saved = null; } }
      await this._open(saved);
      // First run on a phone with several back lenses: prefer the multi-lens camera.
      if (!deviceId && !saved) {
        const preferred = this._preferredCamera();
        if (preferred && preferred.deviceId !== this.deviceId()) {
          try { await this._open(preferred.deviceId); } catch { await this._open(null); }
        }
      }
      this.running = true;
      this.paused = false;
      this._loop();
    }

    async _open(deviceId) {
      this._release();
      const video = {
        width: { ideal: 1920 },
        height: { ideal: 1080 },
        frameRate: { ideal: 30 },
      };
      if (deviceId) video.deviceId = { exact: deviceId };
      else video.facingMode = { ideal: "environment" };
      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: false, video });
      } catch (err) {
        if (deviceId && /Overconstrained|NotFound/i.test(err?.name || "")) {
          try { localStorage.removeItem(CAMERA_KEY); } catch { /* storage unavailable */ }
          return this._open(null);
        }
        throw err;
      }
      this.stream = stream;
      this.track = stream.getVideoTracks()[0];
      this.video.srcObject = stream;
      this.video.setAttribute("playsinline", "");
      this.video.muted = true;
      await this.video.play().catch(() => {});
      await this._waitForSize();
      // Labels are only available after permission is granted.
      try {
        const devices = await navigator.mediaDevices.enumerateDevices();
        this.cameras = devices.filter((d) => d.kind === "videoinput" && isBackCamera(d));
      } catch { this.cameras = []; }
      const caps = this.capabilities();
      if (caps.focusMode?.includes?.("continuous")) {
        this.track.applyConstraints({ advanced: [{ focusMode: "continuous" }] }).catch(() => {});
      }
    }

    _waitForSize() {
      if (this.video.videoWidth) return Promise.resolve();
      return new Promise((resolve) => {
        const done = () => { this.video.removeEventListener("loadedmetadata", done); resolve(); };
        this.video.addEventListener("loadedmetadata", done);
        setTimeout(done, 2500);
      });
    }

    _preferredCamera() {
      for (const re of PREFERRED_LABELS) {
        const hit = this.cameras.find((c) => re.test(c.label));
        if (hit) return hit;
      }
      return null;
    }

    deviceId() {
      return this.track?.getSettings?.().deviceId || "";
    }

    capabilities() {
      try { return this.track?.getCapabilities?.() || {}; } catch { return {}; }
    }

    async useCamera(deviceId) {
      try { localStorage.setItem(CAMERA_KEY, deviceId); } catch { /* storage unavailable */ }
      const wasPaused = this.paused;
      await this._open(deviceId);
      this.paused = wasPaused;
      if (!this.running) { this.running = true; this._loop(); }
    }

    zoomRange() {
      const z = this.capabilities().zoom;
      return z && z.max > 1 ? z : null;
    }

    async setZoom(value) {
      const z = this.zoomRange();
      if (!z) return;
      const v = Math.max(z.min, Math.min(z.max, value));
      await this.track.applyConstraints({ advanced: [{ zoom: v }] });
    }

    currentZoom() {
      return this.track?.getSettings?.().zoom || 1;
    }

    hasTorch() {
      return !!this.capabilities().torch;
    }

    async setTorch(on) {
      if (this.hasTorch()) await this.track.applyConstraints({ advanced: [{ torch: !!on }] });
    }

    pause() { this.paused = true; }
    resume() { this.paused = false; this.recent = { code: "", at: 0 }; }

    _release() {
      if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
      this.stream = null;
      this.track = null;
    }

    async stop() {
      this.running = false;
      this._release();
      if (this.video) this.video.srcObject = null;
    }

    // The part of the video frame that sits under the on-screen box (object-fit: cover).
    _region() {
      const v = this.video;
      const vw = v.videoWidth, vh = v.videoHeight;
      const full = { x: 0, y: 0, w: vw, h: vh };
      if (!this.box || !vw) return full;
      const r = v.getBoundingClientRect();
      const b = this.box.getBoundingClientRect();
      if (!r.width || !b.width) return full;
      const scale = Math.max(r.width / vw, r.height / vh);
      const offX = (r.width - vw * scale) / 2;
      const offY = (r.height - vh * scale) / 2;
      let x = (b.left - r.left - offX) / scale;
      let y = (b.top - r.top - offY) / scale;
      let w = b.width / scale;
      let h = b.height / scale;
      // Pad generously: people rarely line the barcode up exactly.
      const px = w * 0.12, py = h * 0.35;
      x -= px; y -= py; w += 2 * px; h += 2 * py;
      x = Math.max(0, x); y = Math.max(0, y);
      w = Math.min(vw - x, w); h = Math.min(vh - y, h);
      return { x, y, w, h };
    }

    _grab(region, maxWidth) {
      const scale = Math.min(1, maxWidth / region.w);
      const w = Math.max(1, Math.round(region.w * scale));
      const h = Math.max(1, Math.round(region.h * scale));
      if (this.canvas.width !== w || this.canvas.height !== h) {
        this.canvas.width = w;
        this.canvas.height = h;
      }
      this.ctx.drawImage(this.video, region.x, region.y, region.w, region.h, 0, 0, w, h);
      return this.canvas;
    }

    async _loop() {
      while (this.running) {
        const started = performance.now();
        if (!this.paused && !document.hidden && this.video.readyState >= 2 && this.video.videoWidth) {
          this.tick++;
          try {
            // Mostly decode the boxed area at full resolution; now and then the whole
            // frame, in case the barcode is held off to one side.
            const whole = this.tick % 5 === 0;
            const region = whole ? { x: 0, y: 0, w: this.video.videoWidth, h: this.video.videoHeight } : this._region();
            const code = await detectOn(this._grab(region, whole ? 1280 : 1600));
            if (code && this.running && !this.paused) this._confirm(code);
          } catch (err) {
            this.onError?.(err);
          }
        }
        // Aim for ~10 decodes a second without starving slower phones.
        const spent = performance.now() - started;
        await new Promise((r) => setTimeout(r, Math.max(16, 100 - spent)));
      }
    }

    // Native detectors are reliable on one read; for the WASM path require the same
    // code twice within a moment, which filters out the rare partial misread.
    _confirm(code) {
      const now = Date.now();
      if (this.engine === "native" || (this.recent.code === code && now - this.recent.at < 1500)) {
        this.recent = { code: "", at: 0 };
        this.onDetect?.(code);
      } else {
        this.recent = { code, at: now };
      }
    }
  }

  window.BookScanner = BookScanner;
})();
