// Messages between native code, the trusted host page and the realm.
import type { CockpitPlatform, CockpitTheme, FeedView, IntentKind, TileSize, WidgetSettings } from '../../kit/src/index';

/** A feed snapshot as native code delivers it: instants in epoch milliseconds. */
export interface FeedEnvelope {
  observedAt: number | null;
  expectedIntervalSec: number;
  error?: string | null;
  data: unknown;
  history?: { observedAt: number; data: unknown }[];
}

export interface WidgetPlacement {
  slug: string;
  revision: string;
  /** Relative URL of the content-addressed bundle. */
  bundle: string;
  title: string;
  /** Declared feeds and the history depth the manifest asked for. */
  feeds: { id: string; history: number }[];
  size: TileSize;
  settings: WidgetSettings;
  /** Epoch milliseconds the live revision changed: the watchdog quarantines the newest first. */
  changedAt: number;
  selected?: boolean;
}

/** Native → host. */
export type NativeMessage =
  | {
      type: 'snapshot';
      theme: CockpitTheme;
      platform: CockpitPlatform;
      offsetMs: number;
      widgets: WidgetPlacement[];
      feeds: Record<string, FeedEnvelope>;
    }
  | { type: 'feed'; id: string; envelope: FeedEnvelope }
  | { type: 'retry'; slug: string };

/** Host → realm: exactly what each tile may see. */
export interface RealmTile {
  key: string;
  slug: string;
  bundle: string;
  title: string;
  size: TileSize;
  selected: boolean;
  settings: WidgetSettings;
  feeds: Record<string, FeedView>;
}

export type HostToRealm =
  | { type: 'render'; theme: CockpitTheme; platform: CockpitPlatform; offsetMs: number; tiles: RealmTile[] }
  | { type: 'ping'; id: number };

/** Realm → host. Anything else is dropped. */
export type RealmToHost =
  | { type: 'ready' }
  | { type: 'pong'; id: number }
  | { type: 'loaded'; key: string }
  | { type: 'tile_error'; key: string }
  | { type: 'select'; key: string }
  | { type: 'intent'; key: string; kind: string; id: string };

/** Host → native. */
export type HostToNative =
  | { type: 'ready' }
  | { type: 'loaded'; slug: string; revision: string }
  | { type: 'tile_error'; slug: string; revision: string }
  | { type: 'select'; slug: string }
  | { type: 'open'; slug: string; kind: IntentKind; id: string }
  | { type: 'quarantined'; slug: string; revision: string; reason: 'unresponsive' | 'navigated' };
