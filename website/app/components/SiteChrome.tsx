import Image from "next/image";
import Link from "next/link";

export function SiteHeader({ legal = false }: { legal?: boolean }) {
  return (
    <header className="site-header shell">
      <Link className="wordmark" href="/" aria-label="Veetbot home">
        <Image src="/veetbot-icon.svg" width={42} height={42} alt="" />
        <span>VEETBOT</span>
      </Link>
      <nav className="site-nav" aria-label="Primary navigation">
        {legal ? (
          <>
            <Link href="/privacy">Privacy</Link>
            <Link href="/tos">Terms</Link>
          </>
        ) : (
          <>
            <a href="#principles">Principles</a>
            <a href="#gmail">Gmail access</a>
          </>
        )}
        <a href="https://docs.veetbot.com/">Documentation</a>
      </nav>
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="site-footer shell">
      <Link className="wordmark wordmark-footer" href="/" aria-label="Veetbot home">
        <Image src="/veetbot-icon.svg" width={34} height={34} alt="" />
        <span>VEETBOT</span>
      </Link>
      <p>A governed, self-hostable AI agent.</p>
      <nav aria-label="Legal navigation">
        <Link href="/privacy">Privacy</Link>
        <Link href="/tos">Terms</Link>
        <a href="https://docs.veetbot.com/">Docs</a>
      </nav>
    </footer>
  );
}
