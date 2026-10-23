import { Between, Countdown, Metric, StatusPill, Tile } from '@veetbot/cockpit-kit';
import type { PillTone, WidgetProps } from '@veetbot/cockpit-kit';

interface FlightStatus {
  boardingAt: string | null;
  status: 'on_time' | 'delayed' | 'boarding' | 'departed' | 'cancelled';
  gate: string | null;
}

const LABEL: Record<FlightStatus['status'], [string, PillTone]> = {
  on_time: ['On time', 'ok'],
  delayed: ['Delayed', 'attention'],
  boarding: ['Boarding', 'ok'],
  departed: ['Departed', 'neutral'],
  cancelled: ['Cancelled', 'error'],
};

export default function FlightTracker({ feeds, settings, size }: WidgetProps) {
  const feed = feeds['agent.flight-status'];
  const flight = feed?.data as FlightStatus | null | undefined;
  const [label, tone] = flight ? LABEL[flight.status] : ['No status yet', 'neutral' as PillTone];
  return (
    <Tile title={String(settings.flight)} freshness={feed}>
      <Countdown
        target={flight?.boardingAt ? Date.parse(flight.boardingAt) : null}
        label="Until boarding"
        size={size === 'sm' ? 'm' : 'l'}
      />
      <Between>
        <StatusPill tone={tone}>{label}</StatusPill>
        {size === 'sm' ? null : <Metric value={flight?.gate ?? null} size="s" label="Gate" />}
      </Between>
    </Tile>
  );
}
