// The realm: one sandboxed, opaque-origin document that runs every widget.
// React and the kit load once; each tile renders behind its own error
// boundary. The realm has no network: its CSP denies connections and loads
// beyond its own assets, and the primitives CSP does not govern are removed
// here, before any widget code runs.
import * as React from 'react';
import { Component, type CSSProperties, type ReactNode } from 'react';
import * as ReactJSXRuntime from 'react/jsx-runtime';
import { createRoot } from 'react-dom/client';

import * as kit from '../../kit/src/index';
import type { CockpitWidget } from '../../kit/src/index';
import type { HostToRealm, RealmTile, RealmToHost } from './protocol';

const UNGOVERNED = ['RTCPeerConnection', 'webkitRTCPeerConnection', 'RTCDataChannel', 'WebSocket', 'EventSource', 'Worker', 'SharedWorker'];
for (const name of UNGOVERNED) {
  try {
    Object.defineProperty(globalThis, name, { value: undefined, configurable: false, writable: false });
  } catch {
    // Already absent in this engine.
  }
}
try {
  Object.defineProperty(Navigator.prototype, 'sendBeacon', { value: () => false, configurable: false, writable: false });
} catch {
  // Already absent in this engine.
}

const scope = globalThis as unknown as Record<string, unknown>;
scope.React = React;
scope.ReactJSXRuntime = ReactJSXRuntime;
scope.VeetbotKit = kit;

const components = new Map<string, CockpitWidget>();
scope.VeetbotRealm = Object.freeze({
  register(widget: CockpitWidget) {
    const key = (document.currentScript as HTMLScriptElement | null)?.dataset.key;
    if (key && typeof widget === 'function' && !components.has(key)) components.set(key, widget);
  },
});

function post(message: RealmToHost) {
  window.parent.postMessage(message, '*');
}

class TileBoundary extends Component<{ tile: RealmTile; children?: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch() {
    post({ type: 'tile_error', key: this.props.tile.key });
  }

  render() {
    if (!this.state.failed) return this.props.children;
    const { tile } = this.props;
    return (
      <kit.Tile title={tile.title} size={tile.size} freshness={{ observedAt: null, expectedIntervalSec: 60, error: 'This widget stopped working.' }} />
    );
  }
}

const loading = new Set<string>();

function load(tile: RealmTile, rerender: () => void) {
  if (components.has(tile.key) || loading.has(tile.key)) return;
  loading.add(tile.key);
  const script = document.createElement('script');
  script.src = tile.bundle;
  script.dataset.key = tile.key;
  script.onload = () => {
    loading.delete(tile.key);
    if (components.has(tile.key)) post({ type: 'loaded', key: tile.key });
    else post({ type: 'tile_error', key: tile.key });
    rerender();
  };
  script.onerror = () => {
    loading.delete(tile.key);
    post({ type: 'tile_error', key: tile.key });
  };
  document.body.append(script);
}

const root = createRoot(document.getElementById('realm') as HTMLElement);
let latest: Extract<HostToRealm, { type: 'render' }> | null = null;

function draw() {
  if (!latest) return;
  const { theme, platform, offsetMs, columns, tiles } = latest;
  for (const tile of tiles) load(tile, draw);
  root.render(
    <kit.CockpitRoot theme={theme} platform={platform} className="ck-board">
      <kit.CockpitClock offsetMs={offsetMs}>
        <div className="ck-grid" style={columns ? ({ '--ck-cols': columns } as CSSProperties) : undefined}>
          {tiles.map((tile) => {
            const Widget = components.get(tile.key);
            return (
              <div className="ck-slot" data-size={tile.size} key={tile.key}>
                <TileBoundary tile={tile}>
                  <kit.TileHost
                    size={tile.size}
                    selected={tile.selected}
                    onOpen={() => post({ type: 'select', key: tile.key })}
                    onIntent={(intent) => post({ type: 'intent', key: tile.key, kind: intent.kind, id: intent.id })}
                  >
                    {Widget ? (
                      <Widget feeds={tile.feeds} settings={tile.settings} size={tile.size} theme={theme} />
                    ) : (
                      <kit.Tile title={tile.title} freshness={{ observedAt: null, expectedIntervalSec: 60 }} />
                    )}
                  </kit.TileHost>
                </TileBoundary>
              </div>
            );
          })}
        </div>
      </kit.CockpitClock>
    </kit.CockpitRoot>,
  );
}

window.addEventListener('message', (event: MessageEvent<HostToRealm>) => {
  if (event.source !== window.parent) return;
  const message = event.data;
  if (message?.type === 'ping' && typeof message.id === 'number') post({ type: 'pong', id: message.id });
  else if (message?.type === 'render' && Array.isArray(message.tiles)) {
    latest = message;
    draw();
  }
});

post({ type: 'ready' });
