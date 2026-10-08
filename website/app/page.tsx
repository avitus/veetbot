import type { Metadata } from "next";
import Image from "next/image";
import Link from "next/link";
import type { ReactNode } from "react";
import { SiteFooter, SiteHeader } from "./components/SiteChrome";

export const metadata: Metadata = {
  title: { absolute: "Veetbot | Your personal AI assistant" },
  description:
    "Veetbot is a personal AI assistant that sorts your inbox, drafts your replies, starts your morning with a briefing, handles reminders, and remembers the people and details in your life.",
  alternates: { canonical: "/" },
};

function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="24"
      height="24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

const features = [
  {
    title: "Tames your inbox",
    body: "Finds the emails that actually need you, drafts replies that sound like you, and helps you unsubscribe from the clutter.",
    icon: (
      <Icon>
        <path d="M3 13h5l1.5 3h5l1.5-3h5" />
        <path d="M5.5 5h13L21 13v6H3v-6z" />
      </Icon>
    ),
  },
  {
    title: "Starts your morning",
    body: "A short briefing to start the day: what’s on your plate, what came in overnight, and the news you care about.",
    icon: (
      <Icon>
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
      </Icon>
    ),
  },
  {
    title: "Remembers what you tell it",
    body: "Your favorite restaurant, your kid’s allergies, your partner’s coffee order. Tell it once. You can change or delete anything it remembers.",
    icon: (
      <Icon>
        <path d="M6 3h12v18l-6-4-6 4z" />
      </Icon>
    ),
  },
  {
    title: "Keeps track of your people",
    body: "Who’s who, how you know them, and what they mentioned last time—so you can pick up right where you left off.",
    icon: (
      <Icon>
        <circle cx="9" cy="8" r="3.5" />
        <path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5" />
        <path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18 14.8c1.9.7 3.1 2.4 3.5 5.2" />
      </Icon>
    ),
  },
  {
    title: "Reminders in plain words",
    body: "“Remind me every other Tuesday to water the plants.” Just say it, and change it the same way.",
    icon: (
      <Icon>
        <path d="M6 16v-5a6 6 0 0 1 12 0v5l2 2H4z" />
        <path d="M10 21h4" />
      </Icon>
    ),
  },
  {
    title: "Looks things up",
    body: "Searches the web, reads the pages for you, and comes back with an answer instead of a list of links.",
    icon: (
      <Icon>
        <circle cx="11" cy="11" r="7" />
        <path d="M20 20l-4-4" />
      </Icon>
    ),
  },
] as const;

const steps = [
  {
    title: "Just ask",
    body: "Tell Veetbot what you need in your own words, from your iPhone or Mac.",
  },
  {
    title: "It gets to work",
    body: "It checks your mail, searches the web, and keeps going after you put your phone down. You’ll get a notification when it’s done.",
  },
  {
    title: "You have the final say",
    body: "Before Veetbot sends an email or does anything that can’t be undone, it shows you what it’s about to do and waits for your OK.",
  },
] as const;

export default function Home() {
  return (
    <main>
      <SiteHeader />

      <section className="hero shell" aria-labelledby="hero-title">
        <div className="hero-copy">
          <p className="label">Your personal AI assistant</p>
          <h1 id="hero-title">
            An assistant that actually helps.
            <em>And remembers what you told it.</em>
          </h1>
          <p className="hero-lede">
            Veetbot sorts your inbox, drafts your replies, starts your morning
            with a briefing, keeps your reminders, and remembers the people and
            details that matter to you. Just ask in plain words.
          </p>
          <div className="hero-actions">
            <a className="button button-primary" href="#features">
              See what it can do
            </a>
            <a className="button button-secondary" href="#how">
              How it works
            </a>
          </div>
        </div>

        <figure className="chat-card" aria-label="An example conversation with Veetbot">
          <div className="chat-header">
            <Image src="/veetbot-icon.svg" width={36} height={36} alt="" />
            <div>
              <strong>Veetbot</strong>
              <small>Your assistant</small>
            </div>
          </div>
          <div className="chat-thread">
            <p className="bubble bubble-you">Anything I need to deal with today?</p>
            <div className="bubble bubble-veetbot">
              <p>Three things:</p>
              <ul>
                <li>Your landlord needs the lease renewal signed by Friday.</li>
                <li>Sam asked if you’re free for dinner on Saturday.</li>
                <li>Check-in for your Denver flight opens at 4&nbsp;pm.</li>
              </ul>
              <p>I drafted a reply to Sam. Want me to send it?</p>
            </div>
            <div className="draft">
              <small>Draft to Sam</small>
              <p>Saturday works! 7&nbsp;pm at Rosa’s?</p>
              <div className="draft-actions" aria-hidden="true">
                <span className="chip chip-primary">Send</span>
                <span className="chip">Edit</span>
              </div>
            </div>
          </div>
          <figcaption>Nothing gets sent until you say so.</figcaption>
        </figure>
      </section>

      <section className="features shell" id="features" aria-labelledby="features-title">
        <div className="section-intro">
          <p className="label">What it does</p>
          <h2 id="features-title">Help with the everyday stuff.</h2>
          <p>
            Veetbot takes care of the small things that eat up your day, so you
            can get back to the rest of it.
          </p>
        </div>
        <div className="feature-grid">
          {features.map((feature) => (
            <article className="feature" key={feature.title}>
              <span className="feature-icon">{feature.icon}</span>
              <h3>{feature.title}</h3>
              <p>{feature.body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="how shell" id="how" aria-labelledby="how-title">
        <div className="section-intro">
          <p className="label">How it works</p>
          <h2 id="how-title">As easy as asking a friend.</h2>
        </div>
        <ol className="steps">
          {steps.map((step, index) => (
            <li key={step.title}>
              <span className="step-number" aria-hidden="true">{index + 1}</span>
              <h3>{step.title}</h3>
              <p>{step.body}</p>
            </li>
          ))}
        </ol>
      </section>

      <section className="gmail shell" id="gmail" aria-labelledby="gmail-title">
        <div className="gmail-panel">
          <div>
            <p className="label label-light">Works with Gmail</p>
            <h2 id="gmail-title">Your inbox, a lot lighter.</h2>
          </div>
          <div className="gmail-copy">
            <p>
              Connect your Gmail account and Veetbot can read and sort your
              mail, write drafts in your voice, tidy up threads, and send the
              replies you approve. You can disconnect it anytime.
            </p>
            <ul className="pill-list" aria-label="What Veetbot can do with Gmail">
              <li>Sort &amp; summarize</li>
              <li>Draft replies</li>
              <li>Send when you say so</li>
            </ul>
            <Link className="text-link" href="/privacy">
              How Veetbot handles your data <span aria-hidden="true">→</span>
            </Link>
          </div>
        </div>
      </section>

      <section className="closing shell" aria-labelledby="closing-title">
        <Image src="/veetbot-icon.svg" width={64} height={64} alt="" />
        <h2 id="closing-title">Less busywork. More of your day.</h2>
        <p>Veetbot is your personal AI assistant for iPhone and Mac.</p>
      </section>

      <SiteFooter />
    </main>
  );
}
