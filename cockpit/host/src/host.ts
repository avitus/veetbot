// The trusted host page. Native code is its only source of data; it gives the
// realm, for each widget, exactly the declared feeds (at the declared history
// depth) and that widget's own settings, and nothing else. It also runs the
// watchdog: an unresponsive or navigated realm is reloaded and the most
// recently changed widget quarantined.
import type { FeedView, IntentKind } from '../../kit/src/index';
import type { FeedEnvelope, HostToNative, HostToRealm, NativeMessage, RealmTile, RealmToHost, WidgetPlacement } from './protocol';

const HEARTBEAT_MS = 1000;
const INTENT_KINDS: ReadonlySet<string> = new Set(['approval', 'email_thread', 'schedule', 'session']);
const MAX_INTENT_ID = 64;
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

// A declared feed with no snapshot yet; the kit renders it as pending.
const PENDING: FeedView = { observedAt: null, expectedIntervalSec: 0, error: null, data: null, history: [] };

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
        feeds[id] = envelope ? view(envelope, history) : { ...PENDING, history: [] };
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
    case 'intent': {
      // Navigation only: a known kind and a bounded identifier; native code
      // checks the identifier is in this widget's own feeds before opening it.
      const widget = typeof message.key === 'string' ? widgetFor(message.key) : undefined;
      const { kind, id } = message;
      if (widget && typeof kind === 'string' && INTENT_KINDS.has(kind) && typeof id === 'string' && id.length > 0 && id.length <= MAX_INTENT_ID) {
        toNative({ type: 'open', slug: widget.slug, kind: kind as IntentKind, id });
      }
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
