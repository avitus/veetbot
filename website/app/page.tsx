import type { Metadata } from "next";
import Link from "next/link";
import { SiteFooter, SiteHeader } from "./components/SiteChrome";

export const metadata: Metadata = {
  title: { absolute: "Veetbot | Governed AI agent" },
  description:
    "Veetbot is a self-hostable AI agent built for durable work, explicit approvals, inspectable memory, and owner-controlled integrations.",
  alternates: { canonical: "/" },
};

const capabilities = [
  {
    number: "01",
    title: "Acts with a boundary",
    body: "Consequential tool calls pass through deterministic policy and explicit approvals before they touch the world.",
  },
  {
    number: "02",
    title: "Remembers with evidence",
    body: "Long-term memory keeps provenance, supports correction and deletion, and makes recall decisions inspectable.",
  },
  {
    number: "03",
    title: "Keeps going",
    body: "Runs, schedules, checkpoints, and results survive process restarts without turning recovery into guesswork.",
  },
] as const;

export default function Home() {
  return (
    <main>
      <SiteHeader />

      <section className="hero shell" aria-labelledby="hero-title">
        <div className="hero-copy">
          <p className="eyebrow">
            <span>Personal agent</span>
            <span>Owner controlled</span>
          </p>
          <h1 id="hero-title">
            An agent that can act.
            <em>A system you can inspect.</em>
          </h1>
          <p className="hero-lede">
            Veetbot carries useful work across tools, memory, schedules, and
            devices—while keeping consequential actions governed and every run
            open to inspection.
          </p>
          <div className="hero-actions">
            <a className="button button-primary" href="https://docs.veetbot.com/">
              Read the documentation <span aria-hidden="true">↗</span>
            </a>
            <Link className="button button-secondary" href="/privacy">
              How data is handled
            </Link>
          </div>
        </div>

        <div className="trace-card" aria-label="Example of an inspectable Veetbot run">
          <div className="trace-topline">
            <span>RUN / 0087</span>
            <span className="live-dot">Inspectable</span>
          </div>
          <div className="trace-prompt">
            <span className="trace-label">OWNER</span>
            <p>Find the messages that need a reply and draft the important ones.</p>
          </div>
          <ol className="trace-steps">
            <li>
              <span className="step-state step-done">✓</span>
              <div><strong>Search Gmail</strong><small>Read-only tool · completed</small></div>
            </li>
            <li>
              <span className="step-state step-done">✓</span>
              <div><strong>Prepare drafts</strong><small>External write · reviewed</small></div>
            </li>
            <li>
              <span className="step-state step-wait">!</span>
              <div><strong>Send message</strong><small>Waiting for your approval</small></div>
            </li>
          </ol>
          <div className="trace-footer">
            <span>Nothing consequential happens silently.</span>
            <span aria-hidden="true">→</span>
          </div>
        </div>
      </section>

      <section className="principles shell" id="principles" aria-labelledby="principles-title">
        <div className="section-heading">
          <p className="kicker">THE OPERATING IDEA</p>
          <h2 id="principles-title">Capability without surrendering control.</h2>
        </div>
        <div className="capability-grid">
          {capabilities.map((capability) => (
            <article className="capability" key={capability.number}>
              <span>{capability.number}</span>
              <h3>{capability.title}</h3>
              <p>{capability.body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="gmail-section" id="gmail" aria-labelledby="gmail-title">
        <div className="shell gmail-grid">
          <div>
            <p className="kicker kicker-light">GMAIL, WITH GUARDRAILS</p>
            <h2 id="gmail-title">Email access is narrow, visible, and revocable.</h2>
          </div>
          <div className="gmail-copy">
            <p>
              When you connect Gmail, Veetbot can search and read mail, create
              drafts, organize threads, and send messages you approve. It does
              not expose permanent deletion or silently send on your behalf.
            </p>
            <div className="scope-list" aria-label="Gmail access categories">
              <span>Read &amp; triage</span>
              <span>Draft &amp; organize</span>
              <span>Approved send</span>
            </div>
            <Link className="text-link" href="/privacy">
              Read the complete data-use disclosure <span aria-hidden="true">→</span>
            </Link>
          </div>
        </div>
      </section>

      <section className="proof shell" aria-labelledby="proof-title">
        <p className="kicker">DESIGNED IN THE OPEN</p>
        <h2 id="proof-title">The plan, contracts, decisions, and evidence are public.</h2>
        <a className="proof-link" href="https://docs.veetbot.com/">
          <span>Explore the engineering documentation</span>
          <span aria-hidden="true">↗</span>
        </a>
      </section>

      <SiteFooter />
    </main>
  );
}
