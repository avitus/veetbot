import { Tile } from '@veetbot/cockpit-kit';
export default function Pixel() {
  const image = new Image();
  image.src = 'https://evil.example/pixel.gif';
  return <Tile title="image" />;
}
