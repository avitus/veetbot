/* @veetbot/cockpit-kit — Cockpit A2 primitives.
   Every primitive is prop-driven and content-free: widgets pass the values
   their feeds deliver. Freshness flows from Tile to its children through
   context, so a stale tile dims every figure inside it without the widget
   doing anything. Feed strings render as inert text: no links, no actions.
   Ported from the approved MagicPath design (Cockpit · Kit primitives,
   softly-village-9836); styles are in cockpit-kit.css. */
import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from 'react';

/** Bumped on any change that would break a widget built against this kit. */
export const KIT_API = 1;

export type CockpitTheme = 'light' | 'dark' | 'night';
export type CockpitPlatform = 'mac' | 'ipad' | 'iphone';

/* ---------- Root ---------- */

export interface CockpitRootProps {
  theme: CockpitTheme;
  platform: CockpitPlatform;
  className?: string;
  style?: CSSProperties;
  children?: ReactNode;
}

export function CockpitRoot({ theme, platform, className, style, children }: CockpitRootProps) {
  return (
    <div className={['ck-root', className].filter(Boolean).join(' ')} data-ck-theme={theme} data-ck-platform={platform} style={style}>
      {children}
    </div>
  );
}

/* ---------- Clock ---------- */

const NowContext = createContext<number | null>(null);

export interface CockpitClockProps {
  /** Freeze time (galleries, fixtures, screenshots). Omit for a live clock. */
  frozenAt?: number;
  /** Server time minus device time, so freshness never trusts a skewed clock. */
  offsetMs?: number;
  tickMs?: number;
  children?: ReactNode;
}

export function CockpitClock({ frozenAt, offsetMs = 0, tickMs = 1000, children }: CockpitClockProps) {
  const [deviceNow, setDeviceNow] = useState(() => Date.now());
  useEffect(() => {
    if (frozenAt !== undefined) return;
    const id = window.setInterval(() => setDeviceNow(Date.now()), tickMs);
    return () => window.clearInterval(id);
  }, [frozenAt, tickMs]);
  return <NowContext.Provider value={frozenAt ?? deviceNow + offsetMs}>{children}</NowContext.Provider>;
}

export function useNow(): number {
  const now = useContext(NowContext);
  // Outside a CockpitClock there is no shared time source; the realm and every fixture render provide one.
  // eslint-disable-next-line react-hooks/purity
  return now ?? Date.now();
}

/* ---------- Freshness ---------- */

/** What every feed envelope carries: when it was observed and how often it should arrive. */
export interface FeedFreshness {
  observedAt: number | null;
  expectedIntervalSec: number;
  error?: string | null;
}

export type FreshnessLevel = 'live' | 'aging' | 'stale' | 'error' | 'pending';

/** Live within 1.5 intervals, aging to 3, stale beyond. */
export function freshnessLevel(f: FeedFreshness | undefined, now: number): FreshnessLevel {
  if (!f) return 'live';
  if (f.error) return 'error';
  if (f.observedAt == null) return 'pending';
  const age = Math.max(0, (now - f.observedAt) / 1000);
  if (age <= f.expectedIntervalSec * 1.5) return 'live';
  if (age <= f.expectedIntervalSec * 3) return 'aging';
  return 'stale';
}

export function ageSeconds(f: FeedFreshness | undefined, now: number): number | null {
  if (!f || f.observedAt == null) return null;
  return Math.max(0, Math.round((now - f.observedAt) / 1000));
}

export function formatAge(sec: number): string {
  if (sec < 5) return 'now';
  if (sec < 60) return `${sec} s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  if (h < 48) return `${h} h`;
  return `${Math.floor(h / 24)} d`;
}

export function formatInterval(sec: number): string {
  if (sec < 60) return `every ${sec} s`;
  if (sec < 3600) return `every ${Math.round(sec / 60)} min`;
  if (sec < 86400) return `every ${Math.round(sec / 3600)} h`;
  return `every ${Math.round(sec / 86400)} d`;
}

export function formatClock(ms: number): string {
  const d = new Date(ms);
  const h = d.getHours();
  const m = d.getMinutes();
  return `${((h + 11) % 12) + 1}:${String(m).padStart(2, '0')} ${h < 12 ? 'am' : 'pm'}`;
}

interface FreshnessScopeValue {
  level: FreshnessLevel;
  dim: boolean;
}

const FreshnessContext = createContext<FreshnessScopeValue>({ level: 'live', dim: false });

/** Applies a freshness level to every primitive inside it. Tile does this for you. */
export function FreshnessScope({ level, children }: { level: FreshnessLevel; children?: ReactNode }) {
  return <FreshnessContext.Provider value={{ level, dim: level === 'stale' || level === 'error' }}>{children}</FreshnessContext.Provider>;
}

export function useFreshness(): FreshnessScopeValue {
  return useContext(FreshnessContext);
}

export interface FreshnessIndicatorProps {
  freshness?: FeedFreshness;
  /** Short form for tile headers ("12 s"); long form for panels ("Updated 12 s ago"). */
  variant?: 'short' | 'long';
}

export function freshnessText(f: FeedFreshness | undefined, now: number, variant: 'short' | 'long' = 'short'): string {
  const level = freshnessLevel(f, now);
  const age = ageSeconds(f, now);
  const ageText = age == null ? '' : formatAge(age);
  switch (level) {
    case 'pending':
      return variant === 'long' ? 'Waiting for the first update' : 'Waiting';
    case 'error':
      return variant === 'long' ? `Feed failed · last good ${ageText} ago` : `Failed · ${ageText}`;
    case 'stale':
      return variant === 'long' ? `Stale · last update ${ageText} ago` : `Stale · ${ageText}`;
    case 'aging':
      return variant === 'long' ? `Updated ${ageText} ago` : `${ageText} ago`;
    default:
      if (ageText === 'now') return variant === 'long' ? 'Updated just now' : 'now';
      return variant === 'long' ? `Updated ${ageText} ago` : ageText;
  }
}

/**
 * How old a feed is, in words; amber once it is stale.
 *
 * @example
 * <FreshnessIndicator freshness={{ observedAt: 1761156000000, expectedIntervalSec: 300 }} variant="long" />
 */
export function FreshnessIndicator({ freshness, variant = 'short' }: FreshnessIndicatorProps) {
  const now = useNow();
  const level = freshnessLevel(freshness, now);
  const expected = freshness ? formatInterval(freshness.expectedIntervalSec) : '';
  const long = freshnessText(freshness, now, 'long');
  // The arrival ring plays only when a new value lands, never on first paint.
  const [firstSeen] = useState(freshness?.observedAt);
  const arrived = freshness?.observedAt != null && freshness.observedAt !== firstSeen;
  return (
    <span className="ck-fresh" data-level={level} data-arrived={arrived ? 'true' : undefined} title={expected ? `${long} · expected ${expected}` : long}>
      <i className="ck-fresh-dot" key={freshness?.observedAt ?? 'none'} aria-hidden />
      <span>{freshnessText(freshness, now, variant)}</span>
      <span className="ck-sr">{expected ? `, expected ${expected}` : ''}</span>
    </span>
  );
}

/* ---------- Tile ---------- */

export type TileSize = 'sm' | 'md' | 'tall' | 'lg' | 'wide';

interface TileHostValue {
  size?: TileSize;
  selected?: boolean;
  onOpen?: () => void;
}

const TileHostContext = createContext<TileHostValue>({});

/** The realm wraps each widget in this, so its Tile takes the placement's size and the board's selection. */
export function TileHost({ size, selected, onOpen, children }: TileHostValue & { children?: ReactNode }) {
  return <TileHostContext.Provider value={{ size, selected, onOpen }}>{children}</TileHostContext.Provider>;
}

export interface TileProps {
  title: string;
  icon?: ReactNode;
  size?: TileSize;
  freshness?: FeedFreshness;
  /** Amber: something is waiting on the owner. The string is the label ("2 waiting on you"). */
  attention?: string | null;
  /** Explicit empty: the feed is fresh and there is nothing to show. */
  empty?: boolean;
  footer?: ReactNode;
  /** Hide the automatic stale/error notice (for very small tiles). */
  hideNotice?: boolean;
  selected?: boolean;
  onOpen?: () => void;
  className?: string;
  style?: CSSProperties;
  children?: ReactNode;
}

/**
 * The frame every widget renders in: a title, its feed's freshness, and the body.
 *
 * @example
 * <Tile title="Weather" freshness={{ observedAt: 1761156000000, expectedIntervalSec: 300 }}>
 *   <Metric value={64} unit="°" label="Light rain" />
 * </Tile>
 */
export function Tile({ title, icon, size: sizeProp, freshness, attention, empty, footer, hideNotice, selected: selectedProp, onOpen: onOpenProp, className, style, children }: TileProps) {
  const host = useContext(TileHostContext);
  const size = sizeProp ?? host.size ?? 'sm';
  const selected = selectedProp ?? host.selected;
  const onOpen = onOpenProp ?? host.onOpen;
  const now = useNow();
  const level = freshnessLevel(freshness, now);
  const interactive = Boolean(onOpen);
  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (!onOpen) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onOpen();
    }
  };
  const showNotice = !hideNotice && (level === 'stale' || level === 'error');
  return (
    <article
      className={['ck-tile', className].filter(Boolean).join(' ')}
      style={style}
      data-size={size}
      data-level={level}
      data-attention={attention ? 'true' : undefined}
      data-empty={empty ? 'true' : undefined}
      data-selected={selected ? 'true' : undefined}
      data-interactive={interactive ? 'true' : undefined}
      tabIndex={interactive ? 0 : undefined}
      onClick={onOpen}
      onKeyDown={onKeyDown}
      aria-label={`${title}${attention ? `, ${attention}` : ''}, ${freshnessText(freshness, now, 'long')}`}
    >
      <header className="ck-tile-head">
        <span className="ck-tile-icon" aria-hidden>
          {icon}
        </span>
        <h3 className="ck-tile-title" style={{ margin: 0 }}>
          {title}
        </h3>
        <FreshnessIndicator freshness={freshness} />
      </header>
      {attention ? (
        <p className="ck-tile-attn" style={{ margin: 0 }}>
          <span className="ck-row-dot" style={{ background: level === 'stale' || level === 'error' ? 'transparent' : 'var(--amber)', border: level === 'stale' || level === 'error' ? '1.5px solid var(--amber)' : undefined, width: 7, height: 7, borderRadius: 999, boxSizing: 'border-box' }} aria-hidden />
          {attention}
        </p>
      ) : null}
      <FreshnessScope level={level}>
        <div className="ck-tile-body">{level === 'pending' ? <PendingState /> : children}</div>
      </FreshnessScope>
      {showNotice ? level === 'error' ? <ErrorNotice message={freshness?.error ?? 'The feed did not answer.'} freshness={freshness} /> : <StaleNotice freshness={freshness} /> : null}
      {footer && !showNotice ? <footer className="ck-tile-foot">{footer}</footer> : null}
    </article>
  );
}

/* ---------- Metric ---------- */

export interface MetricProps {
  value: string | number | null;
  unit?: string;
  label?: string;
  size?: 'xl' | 'l' | 'm' | 's';
  tone?: 'default' | 'attention' | 'muted';
  /** Shown when value is null. */
  emptyLabel?: string;
}

/**
 * One headline figure with an optional unit and label.
 *
 * @example
 * <Metric value={64} unit="°" label="Light rain" size="xl" />
 */
export function Metric({ value, unit, label, size = 'l', tone = 'default', emptyLabel = 'No reading yet' }: MetricProps) {
  const { dim } = useFreshness();
  const empty = value == null;
  return (
    <div className="ck-metric" data-size={size} data-tone={dim ? 'default' : tone} data-dim={dim ? 'true' : undefined} data-empty={empty ? 'true' : undefined}>
      <span className="ck-metric-value">
        {empty ? '—' : value}
        {!empty && unit ? (
          <span className="ck-metric-unit" data-kind={unit === '°' ? 'deg' : 'text'}>
            {unit}
          </span>
        ) : null}
      </span>
      {empty ? <span className="ck-metric-label">{emptyLabel}</span> : label ? <span className="ck-metric-label">{label}</span> : null}
    </div>
  );
}

/* ---------- Delta ---------- */

export interface DeltaProps {
  value: number | null;
  format?: 'percent' | 'number' | 'points';
  precision?: number;
  period?: string;
  /** Weight, never colour: a fall is not an alarm. */
  emphasis?: boolean;
}

/**
 * A signed change, formatted; weight marks emphasis, never colour.
 *
 * @example
 * <Delta value={0.012} format="percent" period="today" />
 */
export function Delta({ value, format = 'percent', precision = 2, period, emphasis }: DeltaProps) {
  const { dim } = useFreshness();
  if (value == null) {
    return (
      <span className="ck-delta" data-dim="true">
        <span>No change data</span>
      </span>
    );
  }
  const glyph = value > 0 ? '▲' : value < 0 ? '▼' : '–';
  const abs = Math.abs(value).toFixed(precision);
  const text = format === 'percent' ? `${abs}%` : format === 'points' ? `${abs} pts` : abs;
  const word = value > 0 ? 'up' : value < 0 ? 'down' : 'unchanged';
  return (
    <span className="ck-delta" data-emphasis={emphasis ? 'true' : undefined} data-dim={dim ? 'true' : undefined} aria-label={`${word} ${text}${period ? ` ${period}` : ''}`}>
      <span className="ck-delta-glyph" aria-hidden>
        {glyph}
      </span>
      <span aria-hidden>{text}</span>
      {period ? (
        <span className="ck-delta-period" aria-hidden>
          {period}
        </span>
      ) : null}
    </span>
  );
}

/* ---------- Sparkline ---------- */

export interface SparklineProps {
  values: number[];
  height?: number;
  area?: boolean;
  baseline?: number;
  ariaLabel?: string;
}

/**
 * A small line of recent values.
 *
 * @example
 * <Sparkline values={[58, 59, 61, 62, 64]} height={36} />
 */
export function Sparkline({ values, height = 40, area = true, baseline, ariaLabel }: SparklineProps) {
  const { dim } = useFreshness();
  if (values.length < 2) {
    return (
      <div className="ck-spark" style={{ height }} role="img" aria-label={ariaLabel ?? 'No history yet'}>
        <div className="ck-spark-empty" />
      </div>
    );
  }
  const min = Math.min(...values, baseline ?? Infinity);
  const max = Math.max(...values, baseline ?? -Infinity);
  const span = max - min || 1;
  const pad = 3;
  const y = (v: number) => pad + (1 - (v - min) / span) * (height - pad * 2);
  const x = (i: number) => (i / (values.length - 1)) * 100;
  const line = values.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(' ');
  const fill = `${line} L100,${height} L0,${height} Z`;
  const last = values[values.length - 1];
  return (
    <div className="ck-spark" style={{ height }} data-dim={dim ? 'true' : undefined} role="img" aria-label={ariaLabel ?? `Range ${min} to ${max}, latest ${last}`}>
      <svg viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" aria-hidden>
        {area ? <path className="ck-spark-area" d={fill} /> : null}
        {baseline != null ? <line className="ck-spark-base" x1="0" x2="100" y1={y(baseline)} y2={y(baseline)} vectorEffect="non-scaling-stroke" /> : null}
        <path className="ck-spark-line" d={line} vectorEffect="non-scaling-stroke" />
      </svg>
      <span className="ck-spark-dot" style={{ left: '100%', top: y(last) }} aria-hidden />
    </div>
  );
}

/* ---------- Bars ---------- */

export interface BarDatum {
  label: string;
  value: number;
  tone?: 'default' | 'highlight' | 'attention' | 'error';
  display?: string;
}

export interface BarsProps {
  data: BarDatum[];
  orientation?: 'vertical' | 'horizontal';
  height?: number;
  max?: number;
  showLabels?: boolean;
  ariaLabel?: string;
}

/**
 * Labelled bars, vertical or horizontal.
 *
 * @example
 * <Bars data={[{ label: 'Mon', value: 3 }, { label: 'Tue', value: 5, tone: 'highlight' }]} />
 */
export function Bars({ data, orientation = 'vertical', height = 56, max, showLabels = true, ariaLabel }: BarsProps) {
  const { dim } = useFreshness();
  const top = max ?? Math.max(1, ...data.map(d => d.value));
  const summary = ariaLabel ?? data.map(d => `${d.label} ${d.display ?? d.value}`).join(', ');
  if (data.length === 0) {
    return (
      <div className="ck-bars" role="img" aria-label="No data">
        <div className="ck-spark-empty" style={{ height }} />
      </div>
    );
  }
  if (orientation === 'horizontal') {
    return (
      <div className="ck-bars ck-hbars" data-dim={dim ? 'true' : undefined} role="img" aria-label={summary}>
        {data.map(d => (
          <div className="ck-hbar-row" key={d.label} aria-hidden>
            <span className="ck-hbar-label">{d.label}</span>
            <span className="ck-hbar-track">
              <span className="ck-bar" data-tone={dim ? 'default' : d.tone ?? 'highlight'} style={{ display: 'block', width: `${Math.max(2, (d.value / top) * 100)}%` }} />
            </span>
            <span className="ck-hbar-value">{d.display ?? d.value}</span>
          </div>
        ))}
      </div>
    );
  }
  return (
    <div className="ck-bars" data-dim={dim ? 'true' : undefined} role="img" aria-label={summary}>
      <div className="ck-bars-plot" style={{ height }} aria-hidden>
        {data.map(d => (
          <div className="ck-bar-col" key={d.label}>
            <div className="ck-bar" data-tone={d.tone ?? 'default'} style={{ height: `${(d.value / top) * 100}%` }} />
          </div>
        ))}
      </div>
      {showLabels ? (
        <div className="ck-bars-labels" aria-hidden>
          {data.map(d => (
            <span key={d.label}>{d.label}</span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

/* ---------- Gauge ---------- */

export interface GaugeProps {
  value: number | null;
  min?: number;
  max?: number;
  label?: string;
  valueLabel?: string;
  tone?: 'default' | 'attention' | 'error';
  /** Optional marker on the arc, e.g. the owner's threshold. */
  mark?: number;
}

/**
 * A value on an arc between a minimum and a maximum.
 *
 * @example
 * <Gauge value={72} max={100} label="Battery" mark={20} />
 */
export function Gauge({ value, min = 0, max = 100, label, valueLabel, tone = 'default', mark }: GaugeProps) {
  const { dim } = useFreshness();
  const r = 42;
  const cx = 50;
  const cy = 50;
  const len = Math.PI * r;
  const frac = value == null ? 0 : Math.min(1, Math.max(0, (value - min) / (max - min || 1)));
  const markAngle = mark == null ? null : Math.PI * (1 - Math.min(1, Math.max(0, (mark - min) / (max - min || 1))));
  return (
    <div className="ck-gauge" data-tone={tone} data-dim={dim ? 'true' : undefined} data-empty={value == null ? 'true' : undefined} data-labelled={label ? 'true' : undefined} role="meter" aria-valuemin={min} aria-valuemax={max} aria-valuenow={value ?? undefined} aria-label={label}>
      {/* The dial box is exactly the arc's bounds, so the figure can sit on the line between the arc's ends. */}
      <div className="ck-gauge-dial">
        <svg viewBox="0 0 100 54" aria-hidden>
          <path className="ck-gauge-track" d={`M${cx - r},${cy} A${r},${r} 0 0 1 ${cx + r},${cy}`} strokeWidth={8} />
          {value != null ? <path className="ck-gauge-fill" d={`M${cx - r},${cy} A${r},${r} 0 0 1 ${cx + r},${cy}`} strokeWidth={8} strokeDasharray={`${len}`} strokeDashoffset={`${len * (1 - frac)}`} /> : null}
          {markAngle != null ? <line className="ck-gauge-tick" x1={cx + (r - 6) * Math.cos(markAngle)} y1={cy - (r - 6) * Math.sin(markAngle)} x2={cx + (r + 6) * Math.cos(markAngle)} y2={cy - (r + 6) * Math.sin(markAngle)} /> : null}
        </svg>
        <span className="ck-gauge-value">{value == null ? '—' : valueLabel ?? value}</span>
      </div>
      {label ? <span className="ck-gauge-label">{label}</span> : null}
    </div>
  );
}

/* ---------- Countdown ---------- */

export interface CountdownProps {
  target: number | null;
  label?: string;
  size?: 'l' | 'm' | 's';
  /** Within this many seconds the figure turns teal. */
  soonWithinSec?: number;
  /** Use h:mm:ss under an hour instead of words. */
  format?: 'relative' | 'clock';
  tone?: 'default' | 'attention';
  pastLabel?: string;
}

export function formatCountdown(sec: number, format: 'relative' | 'clock'): string {
  const s = Math.abs(Math.round(sec));
  if (format === 'clock' && s < 86400) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}` : `${m}:${String(r).padStart(2, '0')}`;
  }
  if (s < 60) return `${s} s`;
  const min = Math.floor(s / 60);
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  const rm = min % 60;
  if (h < 24) return rm ? `${h} h ${rm} min` : `${h} h`;
  return `${Math.floor(h / 24)} d`;
}

/**
 * Time until (or since) an instant, in words or as a clock.
 *
 * @example
 * <Countdown target={1761158520000} label="Until rain" size="s" />
 */
export function Countdown({ target, label, size = 'm', soonWithinSec = 900, format = 'relative', tone = 'default', pastLabel = 'ago' }: CountdownProps) {
  const now = useNow();
  const { dim } = useFreshness();
  if (target == null) {
    return (
      <div className="ck-count" data-size={size} data-phase="past">
        <span className="ck-count-value">—</span>
        <span className="ck-count-label">Nothing scheduled</span>
      </div>
    );
  }
  const sec = (target - now) / 1000;
  const phase = sec < 0 ? 'past' : sec <= soonWithinSec ? 'soon' : 'later';
  const text = sec < 0 ? `${formatCountdown(sec, 'relative')} ${pastLabel}` : format === 'clock' ? formatCountdown(sec, 'clock') : `in ${formatCountdown(sec, 'relative')}`;
  return (
    <div className="ck-count" data-size={size} data-phase={phase} data-tone={tone} data-dim={dim ? 'true' : undefined}>
      <span className="ck-count-value" role="timer">
        {text}
      </span>
      {label ? <span className="ck-count-label">{label}</span> : null}
    </div>
  );
}

/* ---------- Status pill ---------- */

export type PillTone = 'ok' | 'neutral' | 'attention' | 'error' | 'stale';

/**
 * A short status label; the attention tone is amber.
 *
 * @example
 * <StatusPill tone="attention">2 waiting</StatusPill>
 */
export function StatusPill({ tone = 'neutral', children }: { tone?: PillTone; children: ReactNode }) {
  return (
    <span className="ck-pill" data-tone={tone}>
      {children}
    </span>
  );
}

/* ---------- List and Row ---------- */

export type RowMarker = 'none' | 'ok' | 'neutral' | 'attention' | 'error' | 'stale';

export interface RowProps {
  title: ReactNode;
  detail?: ReactNode;
  value?: ReactNode;
  meta?: ReactNode;
  marker?: RowMarker;
  icon?: ReactNode;
}

/**
 * One line of a list: a title, optional detail, a value and a marker.
 *
 * @example
 * <Row title="Flight EX 100" detail="Gate B12" value="On time" marker="ok" />
 */
export function Row({ title, detail, value, meta, marker = 'none', icon }: RowProps) {
  const { dim } = useFreshness();
  const shown = dim && marker === 'attention' ? 'attention-stale' : dim && marker !== 'none' && marker !== 'error' ? 'stale' : marker;
  return (
    <li className="ck-row" data-marker={shown}>
      <span className="ck-row-marker" aria-hidden>
        {icon ?? (marker === 'none' ? null : <span className="ck-row-dot" />)}
      </span>
      <span className="ck-row-text">
        <span className="ck-row-title">{title}</span>
        {detail ? <span className="ck-row-detail">{detail}</span> : null}
      </span>
      <span className="ck-row-trail">
        {value != null ? <span className="ck-row-value">{value}</span> : null}
        {meta != null ? <span className="ck-row-meta">{meta}</span> : null}
      </span>
    </li>
  );
}

export interface ListProps {
  children?: ReactNode;
  /** Rows the feed holds beyond the ones passed in: rendered as "+N more". */
  more?: number;
  empty?: { title: string; detail?: string } | null;
  ariaLabel?: string;
}

const MORE_LINE = 24;

/**
 * Rows that would be cut by the tile's edge fold into "+N more": a tile never shows half a row.
 *
 * @example
 * <List more={2}>
 *   <Row title="Pay rent" value="Today" marker="attention" />
 * </List>
 */
export function List({ children, more = 0, empty, ariaLabel }: ListProps) {
  const { dim } = useFreshness();
  const ref = useRef<HTMLUListElement>(null);
  const [folded, setFolded] = useState(0);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const box = el.closest('.ck-tile-body') as HTMLElement | null;
    const fit = () => {
      const rows = Array.from(el.children).filter(c => c.classList.contains('ck-row')) as HTMLElement[];
      rows.forEach(r => r.removeAttribute('data-ck-folded'));
      if (!box || rows.length === 0) {
        setFolded(0);
        return;
      }
      const limit = box.getBoundingClientRect().bottom + 0.5;
      const fitting = (reserve: number) => rows.filter(r => r.getBoundingClientRect().bottom <= limit - reserve).length;
      let shown = fitting(0);
      if (shown < rows.length || more > 0) shown = Math.max(1, fitting(MORE_LINE));
      rows.slice(shown).forEach(r => r.setAttribute('data-ck-folded', ''));
      setFolded(rows.length - shown);
    };
    fit();
    if (!box || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(fit);
    ro.observe(box);
    return () => ro.disconnect();
  }, [children, more]);

  if (empty) return <EmptyState title={empty.title} detail={empty.detail} />;
  const hidden = more + folded;
  return (
    <ul className="ck-list" ref={ref} data-dim={dim ? 'true' : undefined} aria-label={ariaLabel}>
      {children}
      {hidden ? <li className="ck-list-more">+{hidden} more</li> : null}
    </ul>
  );
}

/* ---------- Timeline ---------- */

export interface TimelineItem {
  id: string;
  time: string;
  title: string;
  detail?: string;
  state: 'done' | 'now' | 'next' | 'failed' | 'attention';
}

/**
 * Steps in order, each done, now, next, failed or needing attention.
 *
 * @example
 * <Timeline items={[{ id: '1', time: '09:00', title: 'Boarding', state: 'done' }, { id: '2', time: '09:40', title: 'Departure', state: 'next' }]} />
 */
export function Timeline({ items, ariaLabel }: { items: TimelineItem[]; ariaLabel?: string }) {
  const { dim } = useFreshness();
  if (items.length === 0) return <EmptyState title="Nothing on the timeline" />;
  return (
    <ol className="ck-timeline" data-dim={dim ? 'true' : undefined} aria-label={ariaLabel}>
      {items.map(item => (
        <li className="ck-tl-item" key={item.id} data-state={item.state}>
          <span className="ck-tl-time">{item.time}</span>
          <span className="ck-tl-rail" aria-hidden>
            <span className="ck-tl-dot" />
          </span>
          <span className="ck-tl-body">
            <span className="ck-tl-title">{item.title}</span>
            {item.detail ? <span className="ck-tl-detail">{item.detail}</span> : null}
          </span>
        </li>
      ))}
    </ol>
  );
}

/* ---------- Empty, stale, error, pending ---------- */

/**
 * A fresh feed with nothing to show.
 *
 * @example
 * <EmptyState title="Nothing waiting" detail="Approvals appear here." />
 */
export function EmptyState({ title, detail, icon }: { title: string; detail?: string; icon?: ReactNode }) {
  return (
    <div className="ck-empty">
      {icon ? (
        <span className="ck-empty-icon" aria-hidden>
          {icon}
        </span>
      ) : null}
      <span className="ck-empty-title">{title}</span>
      {detail ? <span className="ck-empty-detail">{detail}</span> : null}
    </div>
  );
}

/**
 * The notice a Tile shows when its feed is stale.
 *
 * @example
 * <StaleNotice freshness={{ observedAt: 1761156000000, expectedIntervalSec: 300 }} />
 */
export function StaleNotice({ freshness }: { freshness?: FeedFreshness }) {
  const now = useNow();
  const age = ageSeconds(freshness, now);
  return (
    <p className="ck-notice" data-kind="stale" style={{ margin: 0 }}>
      <span className="ck-notice-glyph" aria-hidden />
      <span>
        Last update {age == null ? 'unknown' : `${formatAge(age)} ago`}
        {freshness ? <span className="ck-notice-extra"> · expected {formatInterval(freshness.expectedIntervalSec)}</span> : null}
      </span>
    </p>
  );
}

/**
 * The notice a Tile shows when its feed reports an error.
 *
 * @example
 * <ErrorNotice message="The feed is unavailable" />
 */
export function ErrorNotice({ message, freshness }: { message: string; freshness?: FeedFreshness }) {
  return (
    <p className="ck-notice" data-kind="error" style={{ margin: 0 }}>
      <span className="ck-notice-glyph" aria-hidden />
      <span>
        {message}
        {freshness?.observedAt ? <span className="ck-notice-extra"> Showing {formatClock(freshness.observedAt)} values.</span> : null}
      </span>
    </p>
  );
}

/**
 * Placeholder lines while a declared feed has no snapshot yet.
 *
 * @example
 * <PendingState lines={2} />
 */
export function PendingState({ lines = 3 }: { lines?: number }) {
  return (
    <div className="ck-pending" aria-label="Waiting for the first update" role="status">
      {Array.from({ length: lines }, (_, i) => (
        <span key={i} style={i > 0 ? { width: `${88 - i * 14}%` } : undefined} />
      ))}
    </div>
  );
}

/* ---------- Layout ---------- */

/**
 * A figure beside its detail: the approvals, people and schedule bodies on the boards.
 *
 * @example
 * <Split>
 *   <Metric value={2} label="waiting" />
 *   <List><Row title="Send the invoice" /></List>
 * </Split>
 */
export function Split({ children }: { children?: ReactNode }) {
  return <div className="ck-split">{children}</div>;
}

/**
 * A headline figure with a chart filling the rest of the line.
 *
 * @example
 * <Headline>
 *   <Metric value={64} unit="°" />
 *   <Sparkline values={[58, 61, 64]} />
 * </Headline>
 */
export function Headline({ children }: { children?: ReactNode }) {
  return <div className="ck-headline">{children}</div>;
}

/**
 * Two items pushed to either edge, such as a figure and its status pill.
 *
 * @example
 * <Between>
 *   <Metric value={3} size="m" />
 *   <StatusPill tone="ok">Up to date</StatusPill>
 * </Between>
 */
export function Between({ children }: { children?: ReactNode }) {
  return <div className="ck-between">{children}</div>;
}

/* ---------- Widget contract ---------- */

/** One declared feed as a widget receives it: the envelope's freshness, its current value and the history the manifest asked for. */
export interface FeedView<T = unknown> extends FeedFreshness {
  data: T | null;
  history: { observedAt: number; data: T }[];
}

/** The owner's private settings for this widget, validated against the manifest's settings schema. */
export type WidgetSettings = Record<string, string | number | boolean>;

export interface WidgetProps {
  feeds: Record<string, FeedView | undefined>;
  settings: WidgetSettings;
  size: TileSize;
  theme: CockpitTheme;
}

export type CockpitWidget = (props: WidgetProps) => ReactNode;
