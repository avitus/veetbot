import { List, Metric, Row, Split, Tile } from '@veetbot/cockpit-kit';
import type { WidgetProps } from '@veetbot/cockpit-kit';

interface Approval {
  kind: string;
  summary: string;
  ageMinutes: number;
}

interface Pending {
  count: number;
  oldestMinutes: number | null;
  items: Approval[];
}

export default function ApprovalsWaiting({ feeds, size }: WidgetProps) {
  const feed = feeds['veetbot.approvals.pending'];
  const pending = feed?.data as Pending | null | undefined;
  const count = pending?.count ?? 0;
  const figure = (
    <Metric
      value={pending ? count : null}
      size="l"
      tone={count > 0 ? 'attention' : 'default'}
      label={pending?.oldestMinutes != null ? `Oldest ${pending.oldestMinutes} min` : 'Nothing waiting'}
    />
  );
  return (
    <Tile title="Approvals" freshness={feed} attention={count > 0 ? `${count} waiting on you` : null}>
      {size === 'sm' || !pending || count === 0 ? (
        figure
      ) : (
        <Split>
          {figure}
          <List more={Math.max(0, count - pending.items.length)}>
            {pending.items.map((item, index) => (
              <Row key={index} marker="attention" title={item.kind} detail={item.summary} meta={`${item.ageMinutes} min`} />
            ))}
          </List>
        </Split>
      )}
    </Tile>
  );
}
