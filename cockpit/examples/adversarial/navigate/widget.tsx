import { Tile } from '@veetbot/cockpit-kit';
// Navigation is the one route the realm cannot close by itself: native code
// blocks the request and the host quarantines the widget.
export default function Navigate() {
  setTimeout(() => {
    location.href = 'https://evil.example/navigate';
  }, 50);
  return <Tile title="navigate" />;
}
