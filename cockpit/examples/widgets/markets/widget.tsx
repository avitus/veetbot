import { Delta, List, Metric, Row, Sparkline, Tile } from '@veetbot/cockpit-kit';
import type { WidgetProps } from '@veetbot/cockpit-kit';

interface Instrument {
  name: string;
  value: string;
  change: number;
  unit: 'percent' | 'points';
}

interface Markets {
  headline: Instrument;
  intraday: number[];
  instruments: Instrument[];
}

export default function MarketsWidget({ feeds, size }: WidgetProps) {
  const feed = feeds['agent.markets'];
  const markets = feed?.data as Markets | null | undefined;
  if (size === 'sm' || !markets) {
    return (
      <Tile title="Markets" freshness={feed}>
        <Metric value={markets?.headline.value ?? null} size="l" label={markets?.headline.name} />
        <Delta value={markets?.headline.change ?? null} period="today" />
      </Tile>
    );
  }
  return (
    <Tile title="Markets" freshness={feed}>
      <Sparkline values={markets.intraday} height={32} />
      <List>
        {markets.instruments.map((instrument) => (
          <Row
            key={instrument.name}
            title={instrument.name}
            value={instrument.value}
            meta={<Delta value={instrument.change} format={instrument.unit} />}
          />
        ))}
      </List>
    </Tile>
  );
}
