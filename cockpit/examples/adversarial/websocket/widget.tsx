import { Tile } from '@veetbot/cockpit-kit';
export default function Socket() {
  try {
    new WebSocket('wss://evil.example/socket');
  } catch {
    // Removed by the realm.
  }
  return <Tile title="websocket" />;
}
