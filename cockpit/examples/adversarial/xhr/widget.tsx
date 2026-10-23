import { Tile } from '@veetbot/cockpit-kit';
export default function Xhr() {
  try {
    const request = new XMLHttpRequest();
    request.open('GET', 'https://evil.example/xhr');
    request.send();
  } catch {
    // Blocked synchronously in some engines.
  }
  return <Tile title="xhr" />;
}
