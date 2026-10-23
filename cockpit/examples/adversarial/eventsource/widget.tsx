import { Tile } from '@veetbot/cockpit-kit';
export default function Events() {
  try {
    new EventSource('https://evil.example/events');
  } catch {
    // Removed by the realm.
  }
  return <Tile title="eventsource" />;
}
