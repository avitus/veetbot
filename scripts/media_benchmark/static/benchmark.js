// Runs one prompt through Reactor and TensorScale at the same instant and
// measures both the same way. See scripts/media_benchmark/README.md.

const REACTOR_TRACKS = [
  { name: "main_video", kind: "video", direction: "recvonly" },
  { name: "main_audio", kind: "audio", direction: "recvonly" },
];
const HISTORY_KEY = "media-benchmark:history:v1";
const FORM_KEY = "media-benchmark:form:v1";
const HISTORY_LIMIT = 200;
const DEFAULT_PROMPT =
  "A red fox trots through fresh snow at dawn, its breath steaming, as the camera tracks low beside it.";
const READY_TIMEOUT_MS = 90_000;
const STEP_TIMEOUT_MS = 10 * 60_000;
const TRAILING_FRAMES_MS = 600;
// A frame whose brightest sampled pixel is below this is treated as blank.
const BLANK_LUMA = 24;
const PROVIDERS = ["reactor", "tensorscale"];
const NAMES = { reactor: "Reactor", tensorscale: "TensorScale" };
const PHASES = [
  { id: "setup", label: "Setup", opacity: 0.35 },
  { id: "generating", label: "Generating", opacity: 1 },
  { id: "delivering", label: "Delivering", opacity: 0.6 },
  { id: "playing", label: "Playing", opacity: 0.22 },
];

const $ = (id) => document.getElementById(id);
const now = () => performance.now();

let config = null;
let reactorSession = null;
let activeRun = null;
let history = loadHistory();
const clocks = {};
const blobUrls = [];

class StopError extends Error {
  constructor() {
    super("Stopped");
  }
}

// ---------------------------------------------------------------- storage

function storageGet(key) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function storageSet(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Private windows and full quotas still run; history just isn't kept.
  }
}

function loadHistory() {
  const stored = storageGet(HISTORY_KEY);
  return Array.isArray(stored) ? stored : [];
}

function saveHistory() {
  history = history.slice(-HISTORY_LIMIT);
  storageSet(HISTORY_KEY, history);
}

// ---------------------------------------------------------------- helpers

function seconds(ms, digits) {
  if (ms == null || !Number.isFinite(ms)) return "—";
  const value = ms / 1000;
  return `${value.toFixed(digits ?? (Math.abs(value) < 10 ? 2 : 1))} s`;
}

function diff(from, to) {
  return from == null || to == null ? null : to - from;
}

function fixed(value, digits, suffix = "") {
  return value == null || !Number.isFinite(value) ? "—" : `${value.toFixed(digits)}${suffix}`;
}

function megabytes(bytes) {
  return bytes == null ? "—" : `${(bytes / 1_000_000).toFixed(bytes < 10_000_000 ? 2 : 1)} MB`;
}

function dollars(value) {
  if (value == null || !Number.isFinite(value)) return "—";
  return `$${value.toFixed(value < 1 ? 3 : 2)}`;
}

function element(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value == null) continue;
    if (name === "class") node.className = value;
    else node.setAttribute(name, value);
  }
  for (const child of children) {
    if (child == null) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function svg(tag, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value != null) node.setAttribute(name, String(value));
  }
  return node;
}

function abortable(promise, signal) {
  if (!signal) return promise;
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new StopError());
      return;
    }
    const onAbort = () => reject(new StopError());
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolve(value);
      },
      (error) => {
        signal.removeEventListener("abort", onAbort);
        reject(error);
      },
    );
  });
}

function delay(ms, signal) {
  return abortable(new Promise((resolve) => setTimeout(resolve, ms)), signal);
}

async function errorMessage(response) {
  try {
    const data = await response.json();
    return data?.error?.message || data?.detail || `${response.status} ${response.statusText}`;
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

async function postJSON(url, body, signal) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) throw new Error(await errorMessage(response));
  return response.json();
}

function parseServerTiming(header) {
  if (!header) return [];
  return header
    .split(",")
    .map((entry) => {
      const [name, ...params] = entry.trim().split(";");
      const timing = { name: name.trim() };
      for (const param of params) {
        const [key, raw] = param.trim().split("=");
        const value = raw?.replace(/^"|"$/g, "");
        if (key === "dur") timing.dur = Number(value);
        if (key === "desc") timing.desc = value;
      }
      return timing;
    })
    .filter((timing) => timing.name);
}

function playbackQuality(video) {
  const quality = video.getVideoPlaybackQuality?.();
  return quality ? { dropped: quality.droppedVideoFrames, total: quality.totalVideoFrames } : null;
}

function median(values) {
  return quantile(values, 0.5);
}

function quantile(values, q) {
  const sorted = values.filter((value) => value != null && Number.isFinite(value)).sort((a, b) => a - b);
  if (!sorted.length) return null;
  if (q === 0.5) {
    const middle = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
  }
  return sorted[Math.min(sorted.length - 1, Math.ceil(q * sorted.length) - 1)];
}

// ---------------------------------------------------------- frame watcher

// Watches what a <video> actually presents. The first non-blank frame on
// screen is "first frame" for both providers, so a black hold between
// Reactor clips or an empty first decode never counts as content.
class FrameWatcher {
  constructor(video) {
    this.video = video;
    const canvas = document.createElement("canvas");
    canvas.width = 32;
    canvas.height = 18;
    this.context = canvas.getContext("2d", { willReadFrequently: true });
    this.active = false;
    this.handle = null;
  }

  start(since, onFirst) {
    this.stop();
    Object.assign(this, {
      since,
      onFirst,
      first: null,
      firstAny: null,
      lastContent: null,
      firstPresented: null,
      lastPresented: null,
      size: null,
    });
    this.active = true;
    if ("requestVideoFrameCallback" in HTMLVideoElement.prototype) {
      const tick = (time, metadata) => {
        if (!this.active) return;
        this.observe(metadata.expectedDisplayTime || time, metadata.presentedFrames, metadata);
        this.handle = this.video.requestVideoFrameCallback(tick);
      };
      this.handle = this.video.requestVideoFrameCallback(tick);
    } else {
      let lastTime = -1;
      let count = 0;
      const poll = () => {
        if (!this.active) return;
        if (this.video.currentTime !== lastTime || this.video.srcObject) {
          lastTime = this.video.currentTime;
          this.observe(now(), (count += 1), {});
        }
        this.handle = requestAnimationFrame(poll);
      };
      this.handle = requestAnimationFrame(poll);
    }
  }

  observe(shown, presented, metadata) {
    if (shown < this.since) return;
    this.firstAny ??= shown;
    if (this.blank()) return;
    if (this.first == null) {
      this.first = shown;
      this.firstPresented = presented;
      this.size = [metadata.width || this.video.videoWidth, metadata.height || this.video.videoHeight];
      this.onFirst?.(shown);
    }
    this.lastContent = shown;
    this.lastPresented = presented;
  }

  blank() {
    try {
      this.context.drawImage(this.video, 0, 0, 32, 18);
      const pixels = this.context.getImageData(0, 0, 32, 18).data;
      let brightest = 0;
      for (let index = 0; index < pixels.length; index += 4) {
        const luma = 0.2126 * pixels[index] + 0.7152 * pixels[index + 1] + 0.0722 * pixels[index + 2];
        if (luma > brightest) brightest = luma;
      }
      return brightest < BLANK_LUMA;
    } catch {
      return false;
    }
  }

  measuredFps() {
    const frames = diff(this.firstPresented, this.lastPresented);
    const span = diff(this.first, this.lastContent);
    return frames && span ? frames / (span / 1000) : null;
  }

  stop() {
    this.active = false;
    if (this.handle == null) return;
    if (this.video.cancelVideoFrameCallback) this.video.cancelVideoFrameCallback(this.handle);
    cancelAnimationFrame(this.handle);
    this.handle = null;
  }
}

// ------------------------------------------------------------- Reactor

let sdkPromise = null;

function loadReactorSdk() {
  sdkPromise ??= import("@reactor-team/js-sdk").catch((error) => {
    sdkPromise = null;
    throw new Error(`Could not load Reactor's SDK from cdn.jsdelivr.net: ${error.message}`);
  });
  return sdkPromise;
}

// Reactor delivers model messages as {type, data}; flatten them the way its
// typed model packages do.
function unwrap(raw) {
  if (raw && typeof raw === "object" && raw.data && typeof raw.data === "object") {
    return { ...raw.data, type: raw.type };
  }
  return raw;
}

class ReactorSession {
  constructor(video) {
    this.video = video;
    this.listeners = new Set();
    this.client = null;
    this.stream = new MediaStream();
    this.lastStats = null;
    this.state = null;
    this.mintMs = null;
    this.timings = null;
  }

  get open() {
    return this.client?.getStatus() === "ready";
  }

  dispatch(message) {
    message.at = now();
    if (message.type === "state_update") this.state = message;
    for (const listener of [...this.listeners]) listener(message);
  }

  waitFor(predicate, timeoutMs = STEP_TIMEOUT_MS) {
    let listener;
    let timer;
    const cancel = () => {
      this.listeners.delete(listener);
      clearTimeout(timer);
    };
    const promise = new Promise((resolve, reject) => {
      listener = (message) => {
        if (!predicate(message)) return;
        cancel();
        resolve(message);
      };
      timer = setTimeout(() => {
        cancel();
        reject(new Error("Reactor stopped answering"));
      }, timeoutMs);
      this.listeners.add(listener);
    });
    promise.catch(() => {});
    return { promise, cancel };
  }

  attach(track) {
    if (!this.stream.getTracks().includes(track)) this.stream.addTrack(track);
    if (this.video.srcObject !== this.stream) this.video.srcObject = this.stream;
    this.video.play().catch(() => {});
  }

  async connect(t0, marks, signal) {
    const sdk = await abortable(loadReactorSdk(), signal);
    setPhase("reactor", "Minting token", "Minting a session token");
    const token = await postJSON("/api/reactor/token", { model: "fast-h3" }, signal);
    marks.token = now() - t0;
    this.mintMs = token.mint_ms;
    setPhase("reactor", "Connecting", "Connecting to Reactor");
    const client = new sdk.Reactor({
      modelName: token.model_name,
      modelTracks: REACTOR_TRACKS,
      jwt: token.jwt,
    });
    this.client = client;
    client.on("message", (raw) => this.dispatch(unwrap(raw)));
    client.on("trackReceived", (_name, track) => this.attach(track));
    client.on("statsUpdate", (stats) => {
      this.lastStats = stats;
      if (stats.connectionTimings) this.timings = stats.connectionTimings;
    });
    client.on("statusChanged", (status) => this.dispatch({ type: "__status", status }));
    client.on("error", (error) => this.dispatch({ type: "__error", error }));
    const ready = this.waitFor(
      (message) => message.type === "__status" && message.status === "ready",
      READY_TIMEOUT_MS,
    );
    await abortable(client.connect(), signal);
    if (client.getStatus() === "ready") ready.cancel();
    else await abortable(ready.promise, signal);
    marks.connected = now() - t0;
  }

  // The SDK never rejects; a refused command comes back as command_error.
  async send(command, data) {
    const reply = await this.client.sendCommand(command, data);
    const message = reply === undefined ? undefined : unwrap(reply);
    if (message?.type === "command_error") throw new Error(`Reactor refused ${command}: ${message.reason}`);
    return message;
  }

  async rtcSnapshot() {
    const peer = this.client?.getPeerConnection?.();
    if (!peer) return null;
    const report = await peer.getStats();
    const snapshot = { at: now(), video: null, audio: null, rtt: null, route: null };
    let selected = null;
    report.forEach((stat) => {
      if (stat.type === "transport" && stat.selectedCandidatePairId) selected = stat.selectedCandidatePairId;
    });
    report.forEach((stat) => {
      if (stat.type === "inbound-rtp" && stat.kind === "video") {
        snapshot.video = {
          bytes: stat.bytesReceived ?? 0,
          dropped: stat.framesDropped ?? 0,
          freezes: stat.freezeCount ?? 0,
          freezeSeconds: stat.totalFreezesDuration ?? 0,
          lost: stat.packetsLost ?? 0,
          received: stat.packetsReceived ?? 0,
          jitter: stat.jitter,
          width: stat.frameWidth,
          height: stat.frameHeight,
        };
      }
      if (stat.type === "inbound-rtp" && stat.kind === "audio") {
        snapshot.audio = {
          bytes: stat.bytesReceived ?? 0,
          lost: stat.packetsLost ?? 0,
          received: stat.packetsReceived ?? 0,
        };
      }
      const chosen = selected ? stat.id === selected : stat.nominated && stat.state === "succeeded";
      if (stat.type === "candidate-pair" && chosen) {
        snapshot.rtt = stat.currentRoundTripTime;
        snapshot.route = report.get(stat.remoteCandidateId)?.candidateType ?? null;
      }
    });
    return snapshot;
  }

  async close() {
    const client = this.client;
    this.client = null;
    this.listeners.clear();
    for (const track of this.stream.getTracks()) this.stream.removeTrack(track);
    if (client) await client.disconnect().catch(() => {});
  }
}

function rtcDelta(before, after) {
  if (!after?.video) return null;
  const video0 = before?.video ?? { bytes: 0, dropped: 0, freezes: 0, freezeSeconds: 0, lost: 0, received: 0 };
  const audio0 = before?.audio ?? { bytes: 0, lost: 0, received: 0 };
  const audio1 = after.audio ?? audio0;
  const lost = after.video.lost - video0.lost + (audio1.lost - audio0.lost);
  const received = after.video.received - video0.received + (audio1.received - audio0.received);
  return {
    bytes: after.video.bytes - video0.bytes + (audio1.bytes - audio0.bytes),
    droppedFrames: after.video.dropped - video0.dropped,
    freezes: after.video.freezes - video0.freezes,
    freezeSeconds: after.video.freezeSeconds - video0.freezeSeconds,
    packetLoss: lost + received > 0 ? lost / (lost + received) : null,
    jitterMs: after.video.jitter != null ? after.video.jitter * 1000 : null,
    rttMs: after.rtt != null ? after.rtt * 1000 : null,
    route: after.route,
    width: after.video.width,
    height: after.video.height,
  };
}

async function runReactor(run) {
  const { t0, settings, signal } = run;
  const marks = {};
  const result = { provider: "reactor", ok: false, marks, warm: false, model: config.reactor.model.name };
  const video = $("reactor-video");
  const watcher = new FrameWatcher(video);
  const waits = [];
  try {
    let session = reactorSession;
    if (settings.warm && session?.open) {
      result.warm = true;
    } else {
      if (session) await session.close();
      session = new ReactorSession(video);
      reactorSession = session;
      await session.connect(t0, marks, signal);
      result.mintMs = session.mintMs;
    }
    setPhase("reactor", "Preparing", "Setting the canvas");
    // A warm session may still hold clips from a stopped run.
    if (result.warm) await session.send("reset", {});
    const canvas = await session.send("set_canvas", { aspect: settings.aspect });
    if (canvas?.type === "canvas_accepted") result.canvas = [canvas.width, canvas.height];
    await session.send("set_autoplay", { enabled: true });
    marks.ready = now() - t0;

    const tag = `bench-${run.id}`;
    const mine = (message) => message.clip?.metadata === tag;
    const wait = (predicate) => {
      const waiter = session.waitFor(predicate);
      waits.push(waiter);
      return waiter.promise;
    };
    const queued = wait((m) => m.type === "clip_queued" && mine(m));
    const generated = wait((m) => m.type === "clip_generated" && mine(m));
    const started = wait((m) => m.type === "clip_started" && mine(m));
    const finished = wait((m) => (m.type === "clip_finished" || m.type === "clip_stopped") && mine(m));
    const failed = wait(
      (m) =>
        (m.type === "clip_failed" && mine(m)) ||
        (m.type === "command_error" && m.command === "enqueue") ||
        (m.type === "__error" && !m.error?.recoverable) ||
        (m.type === "__status" && m.status === "disconnected"),
    ).then((m) => {
      throw new Error(
        m.reason || m.error?.message || (m.type === "__status" ? "Reactor disconnected" : "Reactor failed"),
      );
    });
    failed.catch(() => {});
    const step = (promise) => abortable(Promise.race([promise, failed]), signal);

    watcher.start(now(), (shown) => {
      marks.firstFrame = shown - t0;
      showFirstFrame("reactor", marks.firstFrame);
    });
    const quality0 = playbackQuality(video);
    const rtc0 = await session.rtcSnapshot();
    marks.enqueued = now() - t0;
    setPhase("reactor", "Generating", "Generating the clip");
    session
      .send("enqueue", { prompt: settings.prompt, seconds: settings.duration, seed: settings.seed, metadata: tag })
      .catch((error) => session.dispatch({ type: "clip_failed", clip: { metadata: tag }, reason: error.message }));
    marks.queued = (await step(queued)).at - t0;
    const built = await step(generated);
    marks.generated = built.at - t0;
    result.videoSeconds = built.clip.seconds;
    result.frames = built.clip.frames;
    $("reactor-gen").textContent = seconds(marks.generated - marks.enqueued);
    setPhase("reactor", "Starting playback", "Clip ready, starting playback");
    marks.started = (await step(started)).at - t0;
    setPhase("reactor", "Playing");
    const ended = await step(finished);
    if (ended.type === "clip_stopped") throw new Error("The clip was stopped before it finished");
    await delay(TRAILING_FRAMES_MS, signal);
    watcher.stop();
    const lastShown = watcher.lastContent != null ? watcher.lastContent - t0 : null;
    marks.finished = Math.max(ended.at - t0, lastShown ?? 0);
    marks.firstFrame ??= marks.started;
    const quality1 = playbackQuality(video);
    result.rtc = rtcDelta(rtc0, await session.rtcSnapshot());
    result.droppedFrames = quality0 && quality1 ? quality1.dropped - quality0.dropped : result.rtc?.droppedFrames;
    result.fps = watcher.measuredFps();
    result.size = watcher.size ?? result.canvas ?? (result.rtc ? [result.rtc.width, result.rtc.height] : null);
    result.sessionId = session.client?.getSessionId?.() ?? null;
    result.connection = session.timings;
    result.ok = true;
    setPhase("reactor", "Done");
  } catch (error) {
    result.error = error instanceof StopError ? "Stopped" : error.message;
    setPhase("reactor", result.error, result.error, true);
    if (reactorSession?.open) await reactorSession.send("stop", {}).catch(() => {});
  } finally {
    watcher.stop();
    for (const waiter of waits) waiter.cancel();
    if (!settings.warm && reactorSession) {
      await reactorSession.close();
      reactorSession = null;
    }
    finishClock("reactor", result);
  }
  return result;
}

// ---------------------------------------------------------- TensorScale

function tensorScaleModel(id) {
  return config.tensorscale.models.find((model) => model.id === id);
}

async function runTensorScale(run) {
  const { t0, settings, signal } = run;
  const spec = tensorScaleModel(settings.tsModel);
  const marks = {};
  const result = {
    provider: "tensorscale",
    ok: false,
    marks,
    model: spec.label,
    endpoint: spec.endpoint,
    streaming: spec.streaming,
  };
  const video = $("tensorscale-video");
  const watcher = new FrameWatcher(video);
  try {
    setPhase("tensorscale", "Generating", "Generating on TensorScale");
    marks.requested = now() - t0;
    const response = await fetch("/api/tensorscale/generate", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        model: spec.id,
        prompt: settings.prompt,
        duration_seconds: settings.duration,
        aspect_ratio: settings.aspect,
        resolution: settings.resolution,
        seed: settings.seed,
      }),
      signal,
    });
    marks.headers = now() - t0;
    if (!response.ok) throw new Error(await errorMessage(response));
    result.upstreamHeadersMs = Number(response.headers.get("x-bench-upstream-headers-ms")) || null;
    result.requestId = response.headers.get("x-bench-upstream-request-id");
    result.serverTiming = parseServerTiming(response.headers.get("x-bench-upstream-server-timing"));
    if (!spec.streaming) $("tensorscale-gen").textContent = seconds(marks.headers - marks.requested);
    const expected = Number(response.headers.get("content-length")) || null;
    const reader = response.body.getReader();
    const chunks = [];
    let bytes = 0;
    setPhase("tensorscale", spec.streaming ? "Streaming" : "Downloading");
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      marks.firstByte ??= now() - t0;
      chunks.push(value);
      bytes += value.byteLength;
      overlay(
        "tensorscale",
        expected ? `Downloading ${megabytes(bytes)} of ${megabytes(expected)}` : `Received ${megabytes(bytes)}`,
      );
    }
    marks.downloaded = now() - t0;
    result.bytes = bytes;
    if (spec.streaming) $("tensorscale-gen").textContent = seconds(marks.downloaded - marks.requested);

    const url = URL.createObjectURL(new Blob(chunks, { type: "video/mp4" }));
    blobUrls.push(url);
    watcher.start(now(), (shown) => {
      marks.firstFrame = shown - t0;
      showFirstFrame("tensorscale", marks.firstFrame);
    });
    const quality0 = playbackQuality(video);
    const ended = new Promise((resolve, reject) => {
      video.addEventListener("ended", () => resolve(now()), { once: true });
      video.addEventListener("error", () => reject(new Error("The browser could not play the MP4")), { once: true });
    });
    ended.catch(() => {});
    video.src = url;
    setPhase("tensorscale", "Playing");
    await abortable(
      video.play().catch((error) => {
        // Unmuted playback can need a fresh click; fall back to muted.
        if (error.name !== "NotAllowedError") throw error;
        video.muted = true;
        return video.play();
      }),
      signal,
    );
    const endedAt = await abortable(ended, signal);
    watcher.stop();
    marks.finished = endedAt - t0;
    marks.firstFrame ??= marks.downloaded;
    const quality1 = playbackQuality(video);
    result.droppedFrames = quality0 && quality1 ? quality1.dropped - quality0.dropped : null;
    result.fps = watcher.measuredFps();
    result.videoSeconds = Number.isFinite(video.duration) ? video.duration : null;
    result.size = [video.videoWidth, video.videoHeight];
    result.ok = true;
    setPhase("tensorscale", "Done");
  } catch (error) {
    result.error = error instanceof StopError || error.name === "AbortError" ? "Stopped" : error.message;
    setPhase("tensorscale", result.error, result.error, true);
  } finally {
    watcher.stop();
    finishClock("tensorscale", result);
  }
  return result;
}

// -------------------------------------------------------------- metrics

function derive(result, settings) {
  const marks = result.marks;
  const derived = { ttff: marks.firstFrame ?? null, finished: marks.finished ?? null };
  if (result.provider === "reactor") {
    derived.setup = marks.ready ?? null;
    derived.generation = diff(marks.enqueued, marks.generated);
    derived.delivery = diff(marks.generated, marks.firstFrame);
    const rate = Number(settings.reactorRate);
    derived.cost = rate > 0 && result.videoSeconds ? (rate / 60) * result.videoSeconds : null;
  } else {
    const generated = result.streaming ? marks.downloaded : marks.headers;
    derived.setup = null;
    derived.generation = diff(marks.requested, generated);
    derived.download = diff(marks.headers, marks.downloaded);
    derived.delivery = diff(generated, marks.firstFrame);
    const spec = tensorScaleModel(settings.tsModel);
    const price = spec?.usd_per_second?.[settings.resolution];
    derived.cost = result.ok && price ? price * settings.duration : null;
  }
  derived.speed = result.videoSeconds && derived.generation ? result.videoSeconds / (derived.generation / 1000) : null;
  const bytes = result.provider === "reactor" ? result.rtc?.bytes : result.bytes;
  derived.bytes = bytes ?? null;
  derived.bitrate = bytes && result.videoSeconds ? (bytes * 8) / result.videoSeconds / 1_000_000 : null;
  return derived;
}

function phasesFor(result) {
  const m = result.marks;
  const span = (id, from, to) => (from != null && to != null && to > from ? { id, from, to } : null);
  if (result.provider === "reactor") {
    return [
      span("setup", 0, m.ready),
      span("generating", m.enqueued, m.generated),
      span("delivering", m.generated, m.firstFrame),
      span("playing", m.firstFrame, m.finished),
    ].filter(Boolean);
  }
  const generated = result.streaming ? m.downloaded : m.headers;
  return [
    span("generating", m.requested ?? 0, generated),
    span("delivering", generated, m.firstFrame),
    span("playing", m.firstFrame, m.finished),
  ].filter(Boolean);
}

// Compare two values; lower wins unless higherIsBetter.
function compare(reactor, tensorscale, higherIsBetter = false, word = "sooner") {
  if (reactor == null || tensorscale == null || !Number.isFinite(reactor) || !Number.isFinite(tensorscale)) {
    return null;
  }
  if (reactor <= 0 || tensorscale <= 0) return null;
  const ratio = higherIsBetter ? reactor / tensorscale : tensorscale / reactor;
  if (Math.abs(ratio - 1) < 0.05) return { winner: null, text: "About the same" };
  const winner = ratio > 1 ? "reactor" : "tensorscale";
  const factor = ratio > 1 ? ratio : 1 / ratio;
  return { winner, text: `${NAMES[winner]} ${factor.toFixed(factor < 10 ? 1 : 0)}× ${word}` };
}

// ------------------------------------------------------------- the panels

function setPhase(provider, phase, overlayText, isError = false) {
  const badge = $(`${provider}-phase`);
  badge.textContent = phase;
  badge.classList.toggle("error", isError);
  if (overlayText !== undefined) overlay(provider, overlayText, isError);
  if (phase === "Done" || phase === "Playing") $(`${provider}-overlay`).hidden = true;
}

function overlay(provider, text, isError = false) {
  const box = $(`${provider}-overlay`);
  box.hidden = false;
  box.classList.toggle("error", isError);
  box.replaceChildren(element("span", {}, text));
}

function showFirstFrame(provider, ms) {
  $(`${provider}-overlay`).hidden = true;
  $(`${provider}-ttff`).textContent = seconds(ms);
}

function startClock(provider, t0) {
  const label = $(`${provider}-clock`);
  const tick = () => {
    label.textContent = seconds(now() - t0, 1);
    clocks[provider] = requestAnimationFrame(tick);
  };
  cancelAnimationFrame(clocks[provider]);
  tick();
}

function finishClock(provider, result) {
  cancelAnimationFrame(clocks[provider]);
  const end = result.marks.finished;
  if (end != null) {
    $(`${provider}-clock`).textContent = seconds(end, 1);
    $(`${provider}-done`).textContent = seconds(end);
  }
}

function resetPanels(settings) {
  for (const provider of PROVIDERS) {
    for (const field of ["ttff", "gen", "done"]) $(`${provider}-${field}`).textContent = "—";
    $(`${provider}-clock`).textContent = "0.0 s";
    setPhase(provider, "Waiting", "Starting");
  }
  const tsVideo = $("tensorscale-video");
  tsVideo.removeAttribute("src");
  tsVideo.load();
  while (blobUrls.length > 1) URL.revokeObjectURL(blobUrls.shift());
  const spec = tensorScaleModel(settings.tsModel);
  $("tensorscale-model").textContent = `${spec.label} · ${spec.streaming ? "streamed file" : "file download"}`;
  for (const screen of document.querySelectorAll(".screen")) {
    const [w, h] = settings.aspect.split(":").map(Number);
    screen.style.aspectRatio = `${w} / ${h}`;
  }
}

function skipped(provider, message) {
  setPhase(provider, "Skipped", message, true);
  return { provider, ok: false, skipped: true, error: message, marks: {} };
}

// ------------------------------------------------------------ rendering

function renderAll() {
  const latest = history[history.length - 1];
  renderLatest(latest);
  renderSummary(latest);
  renderHistory();
}

function settingsLabel(settings) {
  const spec = tensorScaleModel(settings.tsModel);
  return `${spec?.label ?? settings.tsModel} · ${settings.duration} s · ${settings.aspect} · ${settings.resolution}`;
}

function sameSettings(a, b) {
  return ["tsModel", "duration", "aspect", "resolution"].every((key) => a[key] === b[key]);
}

function renderLatest(record) {
  const tiles = $("tiles");
  const detail = $("detail");
  const figure = $("timeline-figure");
  tiles.replaceChildren();
  if (!record) {
    detail.hidden = true;
    figure.hidden = true;
    $("latest-sub").textContent = "No runs yet.";
    return;
  }
  const { reactor: r, tensorscale: t, settings } = record;
  const rd = r.derived ?? {};
  const td = t.derived ?? {};
  $("latest-sub").textContent =
    `${new Date(record.at).toLocaleString()} · ${settingsLabel(settings)} · Reactor ${r.warm ? "warm" : "cold"} session`;

  const tile = (label, rv, tv, comparison, format) =>
    element(
      "div",
      { class: "tile" },
      element("span", { class: "label" }, label),
      element("span", { class: "verdict" }, comparison?.text ?? "—"),
      element(
        "span",
        { class: "pair" },
        element("span", {}, element("span", { class: "key reactor" }), `Reactor ${format(rv, r)}`),
        element("span", {}, element("span", { class: "key tensorscale" }), `TensorScale ${format(tv, t)}`),
      ),
    );
  const time = (value, result) => (result.ok || value != null ? seconds(value) : result.error ?? "—");
  const speed = (value) => fixed(value, 2, "× real time");
  const cost = (value, result) =>
    value != null ? dollars(value) : result.provider === "reactor" ? "set a rate" : "—";
  const cheaper =
    rd.cost != null && td.cost != null ? compare(rd.cost, td.cost, false, "cheaper") : null;
  tiles.append(
    tile("Time to first frame", rd.ttff, td.ttff, compare(rd.ttff, td.ttff), time),
    tile("Time to finished clip", rd.finished, td.finished, compare(rd.finished, td.finished), time),
    tile("Generation speed", rd.speed, td.speed, compare(rd.speed, td.speed, true, "faster"), speed),
    tile("Estimated cost", rd.cost, td.cost, cheaper, cost),
  );

  figure.hidden = false;
  renderTimeline(record);
  renderDetail(record);
}

function renderDetail(record) {
  const { reactor: r, tensorscale: t } = record;
  const rd = r.derived ?? {};
  const td = t.derived ?? {};
  const rows = [];
  const row = (label, rv, tv, comparison) => rows.push({ label, rv, tv, comparison });
  const time = (value, result) => (value != null ? seconds(value) : result.ok ? "—" : result.error ?? "—");
  const size = (result) => (result.size?.[0] ? `${result.size[0]}×${result.size[1]}` : "—");

  row("Time to first frame", time(rd.ttff, r), time(td.ttff, t), compare(rd.ttff, td.ttff));
  row(
    "Setup (token and connection)",
    r.warm ? "0 s (session already open)" : connectionText(r, rd),
    "None",
  );
  row("Generation", seconds(rd.generation), seconds(td.generation), compare(rd.generation, td.generation, false, "faster"));
  row("Download", "Streamed", seconds(td.download));
  row("Delivery (generated to first frame)", seconds(rd.delivery), seconds(td.delivery), compare(rd.delivery, td.delivery));
  row("Finished (last frame shown)", time(rd.finished, r), time(td.finished, t), compare(rd.finished, td.finished));
  row("Video length", seconds(r.videoSeconds * 1000), seconds(t.videoSeconds * 1000));
  row(
    "Generation speed",
    fixed(rd.speed, 2, "× real time"),
    fixed(td.speed, 2, "× real time"),
    compare(rd.speed, td.speed, true, "faster"),
  );
  row("Resolution", size(r), size(t));
  row("Frame rate shown", fixed(r.fps, 1, " fps"), fixed(t.fps, 1, " fps"));
  row("Dropped frames", r.droppedFrames ?? "—", t.droppedFrames ?? "—");
  row(
    "Freezes",
    r.rtc ? `${r.rtc.freezes} (${fixed(r.rtc.freezeSeconds, 2, " s")})` : "—",
    "Not applicable to a file",
  );
  row("Data received", megabytes(rd.bytes), megabytes(td.bytes));
  row("Average bitrate", fixed(rd.bitrate, 2, " Mbps"), fixed(td.bitrate, 2, " Mbps"));
  row("Round-trip time", fixed(r.rtc?.rttMs, 0, " ms"), "—");
  row("Jitter", fixed(r.rtc?.jitterMs, 1, " ms"), "—");
  row("Packet loss", r.rtc?.packetLoss != null ? fixed(r.rtc.packetLoss * 100, 2, "%") : "—", "—");
  row("Network route", r.rtc?.route ?? "—", "HTTPS via this server");
  row("Provider-side wait", "—", t.upstreamHeadersMs != null ? `${seconds(t.upstreamHeadersMs)} to response headers` : "—");
  row(
    "Server timing",
    "—",
    t.serverTiming?.length
      ? t.serverTiming.map((entry) => `${entry.desc || entry.name} ${entry.dur != null ? seconds(entry.dur) : ""}`.trim()).join(", ")
      : "—",
  );
  row("Session or request", r.sessionId ?? "—", t.requestId ?? "—");
  row("Estimated cost", rd.cost != null ? dollars(rd.cost) : "Enter a rate above", dollars(td.cost));
  if (!r.ok || !t.ok) row("Status", r.ok ? "Completed" : r.error ?? "Failed", t.ok ? "Completed" : t.error ?? "Failed");

  const body = $("detail").tBodies[0];
  body.replaceChildren(
    ...rows.map(({ label, rv, tv, comparison }) =>
      element(
        "tr",
        {},
        element("th", { scope: "row" }, label),
        element("td", { class: "num" }, rv),
        element("td", { class: "num" }, tv),
        element("td", { class: comparison?.winner ? "faster" : "why" }, comparison?.text ?? ""),
      ),
    ),
  );
  $("detail").hidden = false;
}

function connectionText(result, derived) {
  const parts = [];
  if (result.mintMs != null) parts.push(`token ${seconds(result.mintMs)}`);
  if (result.connection) {
    parts.push(`session ${seconds(result.connection.sessionCreationMs)}`);
    parts.push(`transport ${seconds(result.connection.transportConnectingMs)}`);
  }
  return parts.length ? `${seconds(derived.setup)} (${parts.join(", ")})` : seconds(derived.setup);
}

// ---------------------------------------------------------------- charts

const tooltip = $("tooltip");

function showTooltip(event, value, label) {
  tooltip.replaceChildren(element("strong", {}, value), element("span", {}, label));
  tooltip.hidden = false;
  const pad = 12;
  const { innerWidth, innerHeight } = window;
  const box = tooltip.getBoundingClientRect();
  let x = event.clientX + pad;
  let y = event.clientY + pad;
  if (x + box.width > innerWidth - 8) x = event.clientX - box.width - pad;
  if (y + box.height > innerHeight - 8) y = event.clientY - box.height - pad;
  tooltip.style.left = `${Math.max(8, x)}px`;
  tooltip.style.top = `${Math.max(8, y)}px`;
}

function hideTooltip() {
  tooltip.hidden = true;
}

function hover(node, value, label) {
  node.setAttribute("tabindex", "0");
  node.setAttribute("aria-label", `${label}: ${value}`);
  node.addEventListener("pointermove", (event) => showTooltip(event, value, label));
  node.addEventListener("pointerleave", hideTooltip);
  node.addEventListener("focus", () => {
    const box = node.getBoundingClientRect();
    showTooltip({ clientX: box.left + box.width / 2, clientY: box.top }, value, label);
  });
  node.addEventListener("blur", hideTooltip);
}

let measure = null;
function textWidth(text) {
  measure ??= document.createElement("canvas").getContext("2d");
  measure.font = `500 12px ${getComputedStyle(document.body).fontFamily}`;
  return measure.measureText(text).width;
}

function niceStep(max, target) {
  const raw = max / target;
  const power = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map((step) => step * power).find((step) => raw <= step) ?? raw;
}

function axis(group, scale, max, top, bottom, left, right) {
  const step = niceStep(max, Math.max(2, Math.floor((right - left) / 90)));
  for (let value = 0; value <= max + 1e-9; value += step) {
    const x = scale(value);
    group.append(svg("line", { class: value === 0 ? "baseline" : "gridline", x1: x, x2: x, y1: top, y2: bottom }));
    const label = svg("text", { x, y: bottom + 16, "text-anchor": "middle" });
    label.textContent = `${Number((value / 1000).toFixed(2))} s`;
    group.append(label);
  }
}

function legend(target, items) {
  target.replaceChildren(
    ...items.map(({ label, swatch }) => element("span", {}, swatch, label)),
  );
}

function renderTimeline(record) {
  const host = $("timeline");
  const width = Math.max(320, host.clientWidth || 640);
  const left = 104;
  const right = 24;
  const rowHeight = 52;
  const top = 8;
  const rows = PROVIDERS.map((provider) => record[provider]);
  const end = Math.max(1000, ...rows.flatMap((result) => phasesFor(result).map((phase) => phase.to)));
  const max = end * 1.04;
  const scale = (ms) => left + (ms / max) * (width - left - right);
  const height = top + rows.length * rowHeight + 28;
  const root = svg("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": "Timeline of each provider's phases; the table below lists the same values.",
  });
  const grid = svg("g");
  axis(grid, scale, max, top, top + rows.length * rowHeight, left, width - right);
  root.append(grid);

  rows.forEach((result, index) => {
    const provider = result.provider;
    const y = top + index * rowHeight;
    const name = svg("text", { x: 0, y: y + 34, class: "value" });
    name.textContent = NAMES[provider];
    root.append(name);
    const color = `var(--${provider})`;
    const barTop = y + 20;
    const barHeight = 20;
    const phases = phasesFor(result);
    if (!phases.length) {
      const note = svg("text", { x: left + 4, y: barTop + 14 });
      note.textContent = result.error ?? "No timing";
      root.append(note);
      return;
    }
    for (const phase of phases) {
      const meta = PHASES.find((item) => item.id === phase.id);
      const x0 = scale(phase.from) + 1;
      const x1 = scale(phase.to) - 1;
      const w = Math.max(1, x1 - x0);
      const hit = svg("rect", { class: "hit", x: x0 - 1, y: y + 14, width: w + 2, height: 32 });
      const bar = svg("rect", {
        class: "mark",
        x: x0,
        y: barTop,
        width: w,
        height: barHeight,
        rx: 2,
        fill: color,
        "fill-opacity": meta.opacity,
      });
      hover(hit, seconds(phase.to - phase.from), `${NAMES[provider]} · ${meta.label}, ${seconds(phase.from)} → ${seconds(phase.to)}`);
      root.append(bar, hit);
      const text = `${meta.label} ${seconds(phase.to - phase.from, 1)}`;
      if (textWidth(text) + 12 <= w) {
        const label = svg("text", {
          x: x0 + 6,
          y: barTop + 14,
          class: "inside",
          style: `fill: ${meta.opacity === 1 ? "#ffffff" : "var(--ink)"}`,
          "pointer-events": "none",
        });
        label.textContent = text;
        root.append(label);
      }
    }
    const first = result.marks.firstFrame;
    if (first != null) {
      const x = scale(first);
      root.append(svg("line", { x1: x, x2: x, y1: barTop - 4, y2: barTop + barHeight + 2, stroke: "var(--ink)", "stroke-width": 2 }));
      const label = svg("text", { x: x + 4, y: barTop - 6, class: "value" });
      label.textContent = `first frame ${seconds(first)}`;
      if (x + 4 + textWidth(label.textContent) > width) {
        label.setAttribute("x", String(x - 4));
        label.setAttribute("text-anchor", "end");
      }
      root.append(label);
    }
    if (!result.ok && result.error) {
      const last = phases[phases.length - 1];
      const note = svg("text", { x: Math.min(scale(last.to) + 6, width - right), y: barTop + 14 });
      note.textContent = result.error;
      root.append(note);
    }
  });
  host.replaceChildren(root);
  legend($("timeline-legend"), [
    ...PROVIDERS.map((provider) => ({ label: NAMES[provider], swatch: element("span", { class: `key ${provider}` }) })),
    ...PHASES.map((phase) => ({
      label: phase.label,
      swatch: element("span", { class: "phase-key", style: `opacity: ${phase.opacity}` }),
    })),
  ]);
}

function renderSummary(latest) {
  const figure = $("summary-figure");
  const table = $("summary");
  if (!latest) {
    figure.hidden = true;
    table.hidden = true;
    $("summary-sub").textContent = "Medians appear after the first run. Runs with other settings are counted separately.";
    return;
  }
  const runs = history.filter((record) => sameSettings(record.settings, latest.settings));
  const warm = runs.filter((record) => record.reactor.warm).length;
  $("summary-sub").textContent =
    `${runs.length} run${runs.length === 1 ? "" : "s"} with ${settingsLabel(latest.settings)}` +
    (warm ? ` · ${warm} with a warm Reactor session` : "");
  const values = (provider, key) =>
    runs.filter((record) => record[provider].ok).map((record) => record[provider].derived?.[key]);
  const measures = [
    { key: "ttff", label: "Time to first frame", chart: true },
    { key: "setup", label: "Setup" },
    { key: "generation", label: "Generation", chart: true, word: "faster" },
    { key: "delivery", label: "Delivery" },
    { key: "finished", label: "Finished", chart: true },
    { key: "speed", label: "Generation speed", higher: true, word: "faster", format: (v) => fixed(v, 2, "×") },
  ];
  const rows = measures.map((measure) => {
    const stats = {};
    for (const provider of PROVIDERS) {
      const list = values(provider, measure.key);
      stats[provider] = { median: median(list), p90: quantile(list, 0.9), n: list.filter((v) => v != null).length };
    }
    return { ...measure, stats };
  });
  const format = (measure, value) => (measure.format ? measure.format(value) : seconds(value));
  const success = (provider) => runs.filter((record) => record[provider].ok).length;
  const spend = (provider) => {
    const costs = runs.map((record) => record[provider].derived?.cost).filter((cost) => cost != null);
    return costs.length ? costs.reduce((sum, cost) => sum + cost, 0) : null;
  };

  table.tBodies[0].replaceChildren(
    ...rows.map((measure) => {
      const comparison = compare(
        measure.stats.reactor.median,
        measure.stats.tensorscale.median,
        measure.higher,
        measure.word ?? "sooner",
      );
      return element(
        "tr",
        {},
        element("th", { scope: "row" }, measure.label),
        element("td", { class: "num" }, format(measure, measure.stats.reactor.median)),
        element("td", { class: "num" }, format(measure, measure.stats.reactor.p90)),
        element("td", { class: "num" }, format(measure, measure.stats.tensorscale.median)),
        element("td", { class: "num" }, format(measure, measure.stats.tensorscale.p90)),
        element("td", { class: comparison?.winner ? "faster" : "why" }, comparison?.text ?? ""),
      );
    }),
    element(
      "tr",
      {},
      element("th", { scope: "row" }, "Completed runs"),
      element("td", { class: "num" }, `${success("reactor")} of ${runs.length}`),
      element("td"),
      element("td", { class: "num" }, `${success("tensorscale")} of ${runs.length}`),
      element("td"),
      element("td"),
    ),
    element(
      "tr",
      {},
      element("th", { scope: "row" }, "Estimated spend"),
      element("td", { class: "num" }, dollars(spend("reactor"))),
      element("td"),
      element("td", { class: "num" }, dollars(spend("tensorscale"))),
      element("td"),
      element("td"),
    ),
  );
  table.hidden = false;
  figure.hidden = false;
  renderSummaryChart(rows.filter((row) => row.chart));
}

function renderSummaryChart(rows) {
  const host = $("summary-chart");
  const width = Math.max(320, host.clientWidth || 640);
  const left = 140;
  const right = 72;
  const bar = 16;
  const gap = 4;
  const group = PROVIDERS.length * bar + gap + 20;
  const top = 4;
  const height = top + rows.length * group + 24;
  const all = rows.flatMap((row) => PROVIDERS.map((provider) => row.stats[provider].median ?? 0));
  const max = Math.max(1000, ...all) * 1.05;
  const scale = (ms) => left + (ms / max) * (width - left - right);
  const root = svg("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": "Median seconds per provider; the table below lists the same values.",
  });
  const grid = svg("g");
  axis(grid, scale, max, top, top + rows.length * group - 20, left, width - right);
  root.append(grid);
  rows.forEach((row, index) => {
    const y = top + index * group;
    const name = svg("text", { x: 0, y: y + bar + 4, class: "value" });
    name.textContent = row.label;
    root.append(name);
    PROVIDERS.forEach((provider, slot) => {
      const stats = row.stats[provider];
      if (stats.median == null) return;
      const by = y + slot * (bar + gap);
      const x1 = scale(stats.median);
      const w = Math.max(2, x1 - left);
      // Square at the baseline, 4px round at the data end.
      const path =
        w >= 8
          ? svg("path", {
              class: "mark",
              d: `M${left},${by} H${left + w - 4} a4,4 0 0 1 4,4 V${by + bar - 4} a4,4 0 0 1 -4,4 H${left} Z`,
              fill: `var(--${provider})`,
            })
          : svg("rect", { class: "mark", x: left, y: by, width: w, height: bar, fill: `var(--${provider})` });
      const hit = svg("rect", { class: "hit", x: left, y: by - 2, width: Math.max(w, 24), height: bar + 4 });
      hover(hit, seconds(stats.median), `${NAMES[provider]} · ${row.label} median, p90 ${seconds(stats.p90)}, ${stats.n} run${stats.n === 1 ? "" : "s"}`);
      const value = svg("text", { x: left + w + 6, y: by + 12, class: "value" });
      value.textContent = seconds(stats.median);
      root.append(path, hit, value);
    });
  });
  host.replaceChildren(root);
  legend(
    $("summary-legend"),
    PROVIDERS.map((provider) => ({ label: NAMES[provider], swatch: element("span", { class: `key ${provider}` }) })),
  );
}

function renderHistory() {
  const table = $("history");
  $("history-empty").hidden = history.length > 0;
  table.hidden = history.length === 0;
  const cell = (result, key) => {
    if (!result.ok) return element("td", { class: "num fail" }, result.error ?? "Failed");
    return element("td", { class: "num" }, seconds(result.derived?.[key]));
  };
  table.tBodies[0].replaceChildren(
    ...history
      .map((record, index) =>
        element(
          "tr",
          {},
          element("td", { class: "num" }, index + 1),
          element("td", { class: "num" }, new Date(record.at).toLocaleTimeString()),
          element("td", {}, `${settingsLabel(record.settings)} · seed ${record.settings.seed}${record.reactor.warm ? " · warm" : ""}`),
          cell(record.reactor, "ttff"),
          cell(record.reactor, "finished"),
          cell(record.tensorscale, "ttff"),
          cell(record.tensorscale, "finished"),
          element("td", { class: "prompt" }, record.settings.prompt),
        ),
      )
      .reverse(),
  );
}

// ---------------------------------------------------------------- export

function download(name, type, text) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = element("a", { href: url, download: name });
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function csv() {
  const columns = [
    ["run", (r) => r.id],
    ["at", (r) => r.at],
    ["prompt", (r) => r.settings.prompt],
    ["tensorscale_model", (r) => r.settings.tsModel],
    ["duration_s", (r) => r.settings.duration],
    ["aspect", (r) => r.settings.aspect],
    ["resolution", (r) => r.settings.resolution],
    ["seed", (r) => r.settings.seed],
    ["reactor_warm", (r) => r.reactor.warm ?? ""],
  ];
  for (const provider of PROVIDERS) {
    const d = (key) => (r) => r[provider].derived?.[key] ?? "";
    columns.push(
      [`${provider}_ok`, (r) => r[provider].ok],
      [`${provider}_error`, (r) => r[provider].error ?? ""],
      [`${provider}_first_frame_ms`, d("ttff")],
      [`${provider}_setup_ms`, d("setup")],
      [`${provider}_generation_ms`, d("generation")],
      [`${provider}_delivery_ms`, d("delivery")],
      [`${provider}_finished_ms`, d("finished")],
      [`${provider}_speed_x`, d("speed")],
      [`${provider}_bytes`, d("bytes")],
      [`${provider}_bitrate_mbps`, d("bitrate")],
      [`${provider}_cost_usd`, d("cost")],
      [`${provider}_video_s`, (r) => r[provider].videoSeconds ?? ""],
      [`${provider}_width`, (r) => r[provider].size?.[0] ?? ""],
      [`${provider}_height`, (r) => r[provider].size?.[1] ?? ""],
      [`${provider}_fps`, (r) => r[provider].fps ?? ""],
      [`${provider}_dropped_frames`, (r) => r[provider].droppedFrames ?? ""],
    );
  }
  columns.push(
    ["reactor_rtt_ms", (r) => r.reactor.rtc?.rttMs ?? ""],
    ["reactor_jitter_ms", (r) => r.reactor.rtc?.jitterMs ?? ""],
    ["reactor_packet_loss", (r) => r.reactor.rtc?.packetLoss ?? ""],
    ["reactor_freezes", (r) => r.reactor.rtc?.freezes ?? ""],
    ["tensorscale_request_id", (r) => r.tensorscale.requestId ?? ""],
  );
  const quote = (value) => {
    const text = String(value);
    return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
  };
  return [columns.map(([name]) => name).join(","), ...history.map((r) => columns.map(([, get]) => quote(get(r))).join(","))].join("\n");
}

// ------------------------------------------------------------------ form

function fillSelect(select, options, selected) {
  select.replaceChildren(...options.map(([value, label]) => element("option", { value }, label)));
  select.value = options.some(([value]) => String(value) === String(selected)) ? String(selected) : String(options[0]?.[0] ?? "");
}

function syncForm(saved = {}) {
  const spec = tensorScaleModel($("ts-model").value);
  const reactor = config.reactor.model;
  const durations = spec.durations.filter(
    (value) => value >= Math.floor(reactor.min_seconds) && value <= Math.ceil(reactor.max_seconds),
  );
  fillSelect($("duration"), durations.map((value) => [value, `${value} s`]), saved.duration ?? ($("duration").value || 5));
  fillSelect(
    $("resolution"),
    Object.keys(spec.resolutions).map((tier) => [tier, tier]),
    saved.resolution ?? ($("resolution").value || spec.default_resolution),
  );
  const aspects = spec.resolutions[$("resolution").value].filter((aspect) => reactor.aspects.includes(aspect));
  fillSelect($("aspect"), aspects.map((aspect) => [aspect, aspect]), saved.aspect ?? $("aspect").value);
  $("pair-note").textContent = spec.like_for_like
    ? "Like for like: both providers run MiniMax H3 Fast with the same prompt, length, aspect and seed. Reactor chooses its own canvas size; compare the Resolution row."
    : `Different models: Reactor runs MiniMax H3 Fast and TensorScale runs ${spec.label}. Treat the comparison as indicative.`;
  $("tensorscale-model").textContent = `${spec.label} · ${spec.streaming ? "streamed file" : "file download"}`;
}

function readForm() {
  return {
    prompt: $("prompt").value.trim(),
    tsModel: $("ts-model").value,
    duration: Number($("duration").value),
    aspect: $("aspect").value,
    resolution: $("resolution").value,
    seed: Math.max(0, Math.floor(Number($("seed").value) || 0)),
    runs: Math.min(10, Math.max(1, Math.floor(Number($("runs").value) || 1))),
    warm: $("warm").checked,
    reactorRate: $("reactor-rate").value,
  };
}

function renderKeys() {
  const list = $("keys");
  list.replaceChildren(
    ...[
      ["Reactor key", config.reactor.configured],
      ["TensorScale key", config.tensorscale.configured],
    ].map(([label, ok]) =>
      element("li", { class: ok ? "" : "missing" }, `${label}`, element("strong", {}, ok ? "ready" : "missing")),
    ),
  );
}

// ------------------------------------------------------------------- run

function status(text, isError = false) {
  const line = $("run-status");
  line.textContent = text;
  line.classList.toggle("error", isError);
}

async function runPair(settings, signal) {
  const id = crypto.randomUUID().slice(0, 8);
  resetPanels(settings);
  const t0 = now();
  for (const provider of PROVIDERS) startClock(provider, t0);
  const run = { id, t0, settings, signal };
  const [reactor, tensorscale] = await Promise.all([
    config.reactor.configured ? runReactor(run) : skipped("reactor", "REACTOR_API_KEY is missing"),
    config.tensorscale.configured ? runTensorScale(run) : skipped("tensorscale", "TENSORSCALE_API_KEY is missing"),
  ]);
  for (const provider of PROVIDERS) cancelAnimationFrame(clocks[provider]);
  reactor.derived = derive(reactor, settings);
  tensorscale.derived = derive(tensorscale, settings);
  const { runs: _runs, ...kept } = settings;
  return { id, at: new Date().toISOString(), settings: kept, reactor, tensorscale };
}

async function runBenchmark() {
  const settings = readForm();
  if (!settings.prompt) {
    status("Enter a prompt first.", true);
    return;
  }
  storageSet(FORM_KEY, settings);
  const controller = new AbortController();
  activeRun = controller;
  $("run").disabled = true;
  $("stop").disabled = false;
  try {
    for (let index = 0; index < settings.runs && !controller.signal.aborted; index += 1) {
      status(settings.runs > 1 ? `Run ${index + 1} of ${settings.runs}…` : "Running…");
      const record = await runPair(settings, controller.signal);
      history.push(record);
      saveHistory();
      renderAll();
      if (index < settings.runs - 1) await delay(1500, controller.signal).catch(() => {});
    }
    status(controller.signal.aborted ? "Stopped." : "Done.");
  } catch (error) {
    status(error.message, true);
  } finally {
    activeRun = null;
    $("run").disabled = false;
    $("stop").disabled = true;
    if (!settings.warm && reactorSession) {
      await reactorSession.close();
      reactorSession = null;
    }
  }
}

async function init() {
  const form = storageGet(FORM_KEY) ?? {};
  $("prompt").value = form.prompt || DEFAULT_PROMPT;
  $("seed").value = form.seed ?? 1101;
  $("runs").value = form.runs ?? 1;
  $("warm").checked = form.warm ?? true;
  $("reactor-rate").value = form.reactorRate ?? "";
  try {
    const response = await fetch("/api/config");
    if (!response.ok) throw new Error(await errorMessage(response));
    config = await response.json();
  } catch (error) {
    status(`Could not reach the benchmark server: ${error.message}`, true);
    $("run").disabled = true;
    return;
  }
  renderKeys();
  // Fetch the SDK now so its download never counts against Reactor's setup.
  if (config.reactor.configured) loadReactorSdk().catch((error) => status(error.message, true));
  $("reactor-model").textContent = `${config.reactor.model.label} · real-time stream`;
  fillSelect(
    $("ts-model"),
    config.tensorscale.models.map((model) => [model.id, model.label]),
    form.tsModel ?? config.tensorscale.models[0].id,
  );
  syncForm(form);
  if (!config.reactor.configured && !config.tensorscale.configured) {
    $("run").disabled = true;
    status("Neither key is set. Add REACTOR_API_KEY and TENSORSCALE_API_KEY, then restart the server.", true);
  } else if (!config.reactor.configured || !config.tensorscale.configured) {
    status(`${config.reactor.configured ? "TENSORSCALE_API_KEY" : "REACTOR_API_KEY"} is missing; only one side will run.`);
  }
  renderAll();

  $("ts-model").addEventListener("change", () => syncForm());
  $("resolution").addEventListener("change", () => syncForm());
  $("run").addEventListener("click", runBenchmark);
  $("stop").addEventListener("click", () => {
    activeRun?.abort();
    status("Stopping…");
  });
  for (const button of document.querySelectorAll("button.mute")) {
    button.addEventListener("click", () => {
      const video = $(button.dataset.video);
      video.muted = !video.muted;
      button.textContent = video.muted ? "Unmute" : "Mute";
      button.setAttribute("aria-pressed", String(!video.muted));
    });
  }
  $("export-json").addEventListener("click", () =>
    download("media-benchmark.json", "application/json", JSON.stringify(history, null, 2)),
  );
  $("export-csv").addEventListener("click", () => download("media-benchmark.csv", "text/csv", csv()));
  $("clear-history").addEventListener("click", () => {
    if (!history.length || !confirm("Clear every stored run?")) return;
    history = [];
    saveHistory();
    renderAll();
  });
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const latest = history[history.length - 1];
      if (latest) {
        renderTimeline(latest);
        renderSummary(latest);
      }
    }, 150);
  });
  window.addEventListener("pagehide", () => reactorSession?.close());
}

init();
