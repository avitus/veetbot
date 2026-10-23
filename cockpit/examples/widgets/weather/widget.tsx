import { Countdown, Metric, Sparkline, Tile } from '@veetbot/cockpit-kit';
import type { WidgetProps } from '@veetbot/cockpit-kit';

interface Weather {
  temperature: number;
  summary: string;
  rainStartsAt: string | null;
  hourly: number[];
}

export default function WeatherWidget({ feeds, settings, size }: WidgetProps) {
  const feed = feeds['agent.weather'];
  const weather = feed?.data as Weather | null | undefined;
  const small = size === 'sm';
  return (
    <Tile title={String(settings.place ?? 'Weather')} freshness={feed}>
      <Metric value={weather?.temperature ?? null} unit="°" size={small ? 'l' : 'xl'} label={weather?.summary} />
      <Countdown
        target={weather?.rainStartsAt ? Date.parse(weather.rainStartsAt) : null}
        label={small ? undefined : 'Until rain'}
        size="s"
      />
      {small ? null : <Sparkline values={weather?.hourly ?? []} height={36} />}
    </Tile>
  );
}
