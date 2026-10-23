import { Between, Metric, Split, StatusPill, Tile, Timeline } from '@veetbot/cockpit-kit';
import type { TimelineItem, WidgetProps } from '@veetbot/cockpit-kit';

interface Health {
  onTime: number;
  total: number;
  failed: number;
  items: TimelineItem[];
}

export default function ScheduleHealth({ feeds, size }: WidgetProps) {
  const feed = feeds['veetbot.schedules.health'];
  const health = feed?.data as Health | null | undefined;
  const head = (
    <Between>
      <Metric value={health ? `${health.onTime}/${health.total}` : null} size="m" label="Ran on time today" />
      {health && health.failed > 0 ? <StatusPill tone="error">{`${health.failed} failed`}</StatusPill> : null}
    </Between>
  );
  const items = health?.items ?? [];
  return (
    <Tile title="Schedules" freshness={feed}>
      {size === 'sm' ? (
        head
      ) : size === 'md' ? (
        <Split>
          {head}
          <Timeline items={items.filter((item) => item.state !== 'done').slice(0, 3)} />
        </Split>
      ) : (
        <>
          {head}
          <Timeline items={items} />
        </>
      )}
    </Tile>
  );
}
