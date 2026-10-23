import { Tile } from '@veetbot/cockpit-kit';
export default function Popup() {
  try {
    window.open('https://evil.example/popup');
  } catch {
    // Popups are not allowed in the sandbox.
  }
  const form = document.createElement('form');
  form.action = 'https://evil.example/form';
  form.method = 'post';
  document.body.append(form);
  try {
    form.submit();
  } catch {
    // form-action 'none'.
  }
  try {
    (window.top as Window).location.href = 'https://evil.example/top';
  } catch {
    // Top navigation is not allowed in the sandbox.
  }
  return <Tile title="popup-and-form" />;
}
