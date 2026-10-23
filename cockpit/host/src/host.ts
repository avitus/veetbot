// The trusted host page. Native code is its only source of data; it gives the
// realm, for each widget, exactly the declared feeds (at the declared history
// depth) and that widget's own settings, and nothing else. It also runs the
// watchdog: an unresponsive or navigated realm is reloaded and the most
// recently changed widget quarantined.
import type { FeedView } from '../../kit/src/index';
import type { FeedEnvelope, HostToNative, HostToRealm, NativeMessage, RealmTile, RealmToHost, WidgetPlacement } from './protocol';

const HEARTBEAT_MS = 1000;
const DEADLINE_MS = 5000;

type Snapshot = Extract<NativeMessage, { type: 'snapshot' }>;

declare global {
  interface Window {
    cockpitHost: { apply(message: NativeMessage): void };
    cockpitNativeLog?: HostToNative[];
    webkit?: { messageHandlers?: { cockpit?: { postMessage(message: HostToNative): void } } };
  }
}

const realmUrl = document.querySelector<HTMLMetaElement>('meta[name="cockpit-realm"]')?.content ?? '';
let snapshot: Snapshot | null = null;
let frame: HTMLIFrameElement | null = null;
let ready = false;
let lastPong = Date.now();
let pingId = 0;
const quarantined = new Set<string>();

function toNative(message: HostToNative) {
  const handler = window.webkit?.messageHandlers?.cockpit;
  if (handler) handler.postMessage(message);
  else (window.cockpitNativeLog ??= []).push(message);
}

function keyOf(widget: WidgetPlacement) {
  return `${widget.slug}@${widget.revision}`;
}

function view(envelope: FeedEnvelope, depth: number): FeedView {
  return {
    observedAt: envelope.observedAt,
    expectedIntervalSec: envelope.expectedIntervalSec,
    error: envelope.error ?? null,
    data: envelope.data,
    history: depth > 0 ? (envelope.history ?? []).slice(-depth) : [],
  };
}

export function tilesFor(current: Snapshot, excluded: ReadonlySet<string>): RealmTile[] {
  return current.widgets
    .filter((widget) => !excluded.has(keyOf(widget)))
    .map((widget) => {
      const feeds: Record<string, FeedView> = {};
      for (const { id, history } of widget.feeds) {
        const envelope = current.feeds[id];
        if (envelope) feeds[id] = view(envelope, history);
      }
      return {
        key: keyOf(widget),
        slug: widget.slug,
        bundle: widget.bundle,
        title: widget.title,
        size: widget.size,
        selected: Boolean(widget.selected),
        settings: { ...widget.settings },
        feeds,
      };
    });
}

function send(message: HostToRealm) {
  frame?.contentWindow?.postMessage(message, '*');
}

function render() {
  if (!ready || !snapshot) return;
  send({ type: 'render', theme: snapshot.theme, platform: snapshot.platform, offsetMs: snapshot.offsetMs, tiles: tilesFor(snapshot, quarantined) });
}

function widgetFor(key: string) {
  return snapshot?.widgets.find((widget) => keyOf(widget) === key);
}

function quarantineNewest(reason: 'unresponsive' | 'navigated') {
  const candidates = (snapshot?.widgets ?? []).filter((widget) => !quarantined.has(keyOf(widget)));
  const newest = candidates.sort((a, b) => b.changedAt - a.changedAt)[0];
  if (newest) {
    quarantined.add(keyOf(newest));
    toNative({ type: 'quarantined', slug: newest.slug, revision: newest.revision, reason });
  }
  mountRealm();
}

function mountRealm() {
  frame?.remove();
  ready = false;
  lastPong = Date.now();
  const next = document.createElement('iframe');
  next.setAttribute('sandbox', 'allow-scripts');
  next.setAttribute('title', 'Cockpit');
  next.className = 'cockpit-realm';
  let loads = 0;
  next.addEventListener('load', () => {
    loads += 1;
    // The realm document loads once. A second load is the realm navigating itself.
    if (loads > 1 && frame === next) quarantineNewest('navigated');
  });
  next.src = realmUrl;
  frame = next;
  document.body.append(next);
}

function fromRealm(event: MessageEvent<RealmToHost>) {
  if (!frame || event.source !== frame.contentWindow) return;
  const message = event.data;
  switch (message?.type) {
    case 'ready':
      ready = true;
      lastPong = Date.now();
      toNative({ type: 'ready' });
      render();
      break;
    case 'pong':
      if (message.id === pingId) lastPong = Date.now();
      break;
    case 'loaded':
    case 'tile_error': {
      const widget = typeof message.key === 'string' ? widgetFor(message.key) : undefined;
      if (widget) toNative({ type: message.type, slug: widget.slug, revision: widget.revision });
      break;
    }
    case 'select': {
      const widget = typeof message.key === 'string' ? widgetFor(message.key) : undefined;
      if (widget) toNative({ type: 'select', slug: widget.slug });
      break;
    }
    default:
      break;
  }
}

window.cockpitHost = {
  apply(message: NativeMessage) {
    if (message.type === 'snapshot') {
      snapshot = message;
      const live = new Set(message.widgets.map(keyOf));
      for (const key of [...quarantined]) if (!live.has(key)) quarantined.delete(key);
    } else if (message.type === 'feed' && snapshot) {
      snapshot = { ...snapshot, feeds: { ...snapshot.feeds, [message.id]: message.envelope } };
    } else if (message.type === 'retry') {
      for (const key of [...quarantined]) if (key.startsWith(`${message.slug}@`)) quarantined.delete(key);
    }
    render();
  },
};

window.addEventListener('message', fromRealm);
window.setInterval(() => {
  if (!ready) {
    if (Date.now() - lastPong > DEADLINE_MS) quarantineNewest('unresponsive');
    return;
  }
  if (Date.now() - lastPong > DEADLINE_MS) {
    quarantineNewest('unresponsive');
    return;
  }
  pingId += 1;
  send({ type: 'ping', id: pingId });
}, HEARTBEAT_MS);

mountRealm();
