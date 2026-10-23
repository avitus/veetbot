import { Tile } from '@veetbot/cockpit-kit';
export default function Evaluation() {
  const attempts = [
    () => (0, eval)("fetch('https://evil.example/eval')"),
    () => new Function("fetch('https://evil.example/function')")(),
    () => [].constructor.constructor("fetch('https://evil.example/constructor')")(),
    () => setTimeout("fetch('https://evil.example/timer')", 0),
  ];
  for (const attempt of attempts) {
    try {
      attempt();
    } catch {
      // Refused without unsafe-eval.
    }
  }
  return <Tile title="evaluation" />;
}
