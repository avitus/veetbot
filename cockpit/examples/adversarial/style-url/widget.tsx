import { Tile } from '@veetbot/cockpit-kit';
export default function StyleUrl() {
  return (
    <Tile title="style-url">
      <div style={{ backgroundImage: 'url(https://evil.example/background.png)', width: 10, height: 10 }} />
    </Tile>
  );
}
