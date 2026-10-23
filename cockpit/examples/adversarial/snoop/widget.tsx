import { Tile } from '@veetbot/cockpit-kit';
import type { WidgetProps } from '@veetbot/cockpit-kit';

// Renders everything it was given, so the test can prove what the host withheld.
export default function Snoop({ feeds, settings }: WidgetProps) {
  return <Tile title={JSON.stringify({ feeds: Object.keys(feeds), history: feeds['agent.weather']?.history.length, settings })} />;
}
