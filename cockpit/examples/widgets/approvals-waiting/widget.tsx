import { List, Metric, Row, Split, Tile, useNow } from '@veetbot/cockpit-kit';
import type { WidgetProps } from '@veetbot/cockpit-kit';

interface Approval {
  kind: string;
  summary: string;
  requestedAt: string;
  expiresAt: string;
  risk: string;
}

interface Pending {
  count: number;
  oldestAt: string | null;
  items: Approval[];
}

export default function ApprovalsWaiting({ feeds, size }: WidgetProps) {
  const now = useNow();
  const feed = feeds['veetbot.approvals.pending'];
  const pending = feed?.data as Pending | null | undefined;
  const count = pending?.count ?? 0;
  const minutes = (at: string) => Math.max(0, Math.round((now - Date.parse(at)) / 60_000));
  const figure = (
    <Metric
      value={pending ? count : null}
      size="l"
      tone={count > 0 ? 'attention' : 'default'}
      label={pending?.oldestAt != null ? `Oldest ${minutes(pending.oldestAt)} min` : 'Nothing waiting'}
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
              <Row key={index} marker="attention" title={item.kind} detail={item.summary} meta={`${minutes(item.requestedAt)} min`} />
            ))}
          </List>
        </Split>
      )}
    </Tile>
  );
}
