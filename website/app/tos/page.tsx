import type { Metadata } from "next";
import Link from "next/link";
import { SiteFooter, SiteHeader } from "../components/SiteChrome";

export const metadata: Metadata = {
  title: "Terms of Service",
  description: "Basic terms governing use of Veetbot and its optional integrations.",
  alternates: { canonical: "/tos" },
};

export default function TermsOfService() {
  return (
    <main>
      <SiteHeader legal />
      <article className="legal shell">
        <header className="legal-hero">
          <p className="kicker">LEGAL / SERVICE TERMS</p>
          <h1>Terms of Service</h1>
          <p className="legal-intro">
            These terms govern use of the Veetbot website, software, and
            optional service integrations.
          </p>
          <p className="effective-date">Effective September 2, 2026</p>
        </header>

        <div className="legal-layout">
          <aside className="legal-summary" aria-label="Terms summary">
            <p>The short version</p>
            <ul>
              <li>Use only accounts and data you are authorized to access.</li>
              <li>Review consequential actions before approving them.</li>
              <li>Do not use Veetbot for abuse, spam, or unlawful activity.</li>
              <li>The software is provided without a service guarantee.</li>
            </ul>
          </aside>

          <div className="legal-copy">
            <section>
              <h2>1. Acceptance</h2>
              <p>
                By accessing or using Veetbot, you agree to these Terms of
                Service and the <Link href="/privacy">Privacy Policy</Link>. If
                you do not agree, do not use Veetbot or authorize Veetbot to
                access an external account.
              </p>
            </section>

            <section>
              <h2>2. What Veetbot provides</h2>
              <p>
                Veetbot is a self-hostable AI agent platform that can use tools,
                keep context and memory, execute scheduled work, request
                approvals, and connect to optional services. Features may be
                experimental, incomplete, changed, suspended, or discontinued.
              </p>
            </section>

            <section>
              <h2>3. Your accounts and authority</h2>
              <p>
                You may connect only accounts, systems, and data that you own or
                are authorized to use. You are responsible for the instructions
                you provide, the permissions you grant, the configuration of
                your deployment, and activity performed through your accounts.
                Keep credentials confidential and promptly revoke access you no
                longer intend to grant.
              </p>
            </section>

            <section>
              <h2>4. Gmail integration</h2>
              <p>
                When you authorize Veetbot through Google OAuth, you permit the
                Gmail operations described on the consent screen and in the
                Privacy Policy. Depending on the scopes you approve, Veetbot may
                read mail, create drafts, organize threads, move threads to or
                from trash, and send messages.
              </p>
              <p>
                You are responsible for reviewing proposed recipients, content,
                and consequences before granting approval. Delivery, provider
                acceptance, thread state, and the outcome of a disrupted network
                request cannot always be guaranteed. When an outcome is
                uncertain, verify it directly in Gmail before trying again.
              </p>
            </section>

            <section>
              <h2>5. Acceptable use</h2>
              <p>You must not use Veetbot to:</p>
              <ul>
                <li>violate law, another person&apos;s rights, or contractual duties;</li>
                <li>send spam, phishing, deceptive, abusive, or unsolicited bulk messages;</li>
                <li>gain unauthorized access to accounts, systems, or data;</li>
                <li>circumvent provider restrictions, quotas, safety controls, or approvals;</li>
                <li>distribute malware or intentionally disrupt a service; or</li>
                <li>misrepresent automated content or activity in a deceptive manner.</li>
              </ul>
            </section>

            <section>
              <h2>6. Third-party services</h2>
              <p>
                Veetbot may interoperate with Google, AI model providers, hosting
                platforms, and other third-party services. Their terms, privacy
                policies, availability, pricing, quotas, and enforcement apply
                independently. Veetbot does not control and is not responsible
                for those services.
              </p>
            </section>

            <section>
              <h2>7. Data and privacy</h2>
              <p>
                The <Link href="/privacy">Privacy Policy</Link> describes how
                Veetbot accesses, uses, stores, and shares data. You are
                responsible for ensuring that your use and deployment comply
                with applicable privacy, employment, confidentiality, and data
                protection obligations.
              </p>
            </section>

            <section>
              <h2>8. Software and content</h2>
              <p>
                Veetbot&apos;s source code is governed by the license distributed
                with the repository. You retain your rights in the content and
                data you provide. You grant only the permissions needed to
                process that content to operate the features you request.
              </p>
            </section>

            <section>
              <h2>9. No professional advice</h2>
              <p>
                Veetbot output may be incomplete or incorrect and is not legal,
                medical, financial, or other professional advice. Verify
                important information and use qualified professionals when
                appropriate.
              </p>
            </section>

            <section>
              <h2>10. Disclaimers</h2>
              <p>
                To the maximum extent permitted by law, Veetbot is provided
                “as is” and “as available,” without warranties of accuracy,
                reliability, availability, fitness for a particular purpose,
                non-infringement, or uninterrupted operation. You assume the
                risks of using AI-generated output and automated tools.
              </p>
            </section>

            <section>
              <h2>11. Limitation of liability</h2>
              <p>
                To the maximum extent permitted by law, the Veetbot project and
                its operator will not be liable for indirect, incidental,
                special, consequential, exemplary, or punitive damages, or for
                lost data, revenue, profits, business, or opportunities arising
                from use of or inability to use Veetbot. Rights that cannot
                legally be limited remain unaffected.
              </p>
            </section>

            <section>
              <h2>12. Suspension and termination</h2>
              <p>
                Access may be suspended or terminated to protect users or
                systems, comply with law or provider requirements, address a
                security risk, or respond to a violation of these terms. You may
                stop using Veetbot and revoke connected accounts at any time.
              </p>
            </section>

            <section>
              <h2>13. Changes and contact</h2>
              <p>
                These terms may change as Veetbot evolves. The effective date
                above will be updated when changes are published. For terms or
                account questions, use the support address displayed on
                Veetbot&apos;s Google OAuth consent screen. For non-sensitive
                public questions, use the{
                " "
                }<a href="https://github.com/avitus/veetbot/issues">project issue tracker</a>.
              </p>
            </section>
          </div>
        </div>
      </article>
      <SiteFooter />
    </main>
  );
}
