import { Tile } from '@veetbot/cockpit-kit';
export default function Injected() {
  const add = (tag: string, attributes: Record<string, string>) => {
    const element = document.createElement(tag);
    for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
    document.body.append(element);
  };
  add('script', { src: 'https://evil.example/script.js' });
  add('link', { rel: 'stylesheet', href: 'https://evil.example/style.css' });
  add('iframe', { src: 'https://evil.example/frame' });
  add('img', { src: 'https://evil.example/img.png' });
  add('object', { data: 'https://evil.example/object' });
  const style = document.createElement('style');
  style.textContent = "@font-face { font-family: x; src: url(https://evil.example/font.woff); } body { font-family: x; background: url(https://evil.example/css.png); }";
  document.head.append(style);
  return <Tile title="injected-elements" />;
}
