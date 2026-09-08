import type { Metadata } from "next";
import Link from "next/link";
import { SiteFooter, SiteHeader } from "../components/SiteChrome";

export const metadata: Metadata = {
  title: "Privacy Policy",
  description:
    "How Veetbot accesses, uses, stores, shares, and protects data, including data obtained through the Gmail API.",
  alternates: { canonical: "/privacy" },
};

/** Render the public policy governing Veetbot's website and Gmail integration. */
export default function PrivacyPolicy() {
  return (
    <main>
      <SiteHeader legal />
      <article className="legal shell">
        <header className="legal-hero">
          <p className="kicker">LEGAL / DATA USE</p>
          <h1>Privacy Policy</h1>
          <p className="legal-intro">
            This policy explains how Veetbot handles information on its public
            website and in its optional Gmail integration.
          </p>
          <p className="effective-date">Effective September 7, 2026</p>
        </header>

        <div className="legal-layout">
          <aside className="legal-summary" aria-label="Privacy summary">
            <p>In plain language</p>
            <ul>
              <li>Gmail access is optional and requires your consent.</li>
              <li>Veetbot uses mail data to perform the tasks you request.</li>
              <li>Veetbot does not sell Google user data or use it for ads.</li>
              <li>You can revoke access and request deletion.</li>
            </ul>
          </aside>

          <div className="legal-copy">
            <section>
              <h2>1. Scope and who controls data</h2>
              <p>
                Veetbot is a self-hostable AI agent. The operator of a Veetbot
                deployment controls that deployment and its stored records.
                This policy describes the current Veetbot public website and
                the owner-operated Gmail integration. It does not replace the
                policies of Google, an AI model provider, or another service
                you choose to connect.
              </p>
            </section>

            <section>
              <h2>2. Public website data</h2>
              <p>
                The public website does not provide accounts, forms, behavioral
                analytics, advertising, or advertising cookies. Hosting and
                security infrastructure may process ordinary request data such
                as IP address, browser type, requested URL, and timestamps to
                deliver the site, prevent abuse, and diagnose failures.
              </p>
            </section>

            <section>
              <h2>3. Google data Veetbot accesses</h2>
              <p>
                Veetbot requests three separate Gmail permissions. Each is used
                only when its corresponding integration is enabled:
              </p>
              <dl className="scope-disclosure">
                <div>
                  <dt><code>gmail.readonly</code></dt>
                  <dd>
                    Search threads and read message metadata, headers, labels,
                    snippets, and plain-text message bodies. HTML is reduced to
                    text. Attachment names, media types, and sizes may be seen,
                    but attachment contents are not downloaded.
                  </dd>
                </div>
                <div>
                  <dt><code>gmail.modify</code></dt>
                  <dd>
                    Google&apos;s grant permits Veetbot to read, compose, and send
                    all Gmail messages. Veetbot restricts the separate write
                    integration to creating drafts; adding or removing labels;
                    archiving or marking threads read; and moving threads to or
                    from Gmail trash. Sending uses the separate{" "}
                    <code>gmail.send</code> integration and approval path.
                  </dd>
                </div>
                <div>
                  <dt><code>gmail.send</code></dt>
                  <dd>
                    Send a plain-text message after the complete proposed
                    message has been presented for approval.
                  </dd>
                </div>
              </dl>
              <p>
                Veetbot does not use these permissions to permanently delete
                Gmail messages, download attachments, access calendars, or
                connect additional mailboxes without a new authorization.
              </p>
              <p>
                Generated summaries, classifications, drafts, approval records,
                and other derived output are treated as Google user data under
                this policy. Veetbot does not create cross-user aggregate or
                anonymized Gmail datasets.
              </p>
            </section>

            <section>
              <h2>4. How Google data is used</h2>
              <p>Google data is used only to provide user-facing features, including:</p>
              <ul>
                <li>searching, reading, and summarizing mail you ask Veetbot to review;</li>
                <li>preparing drafts and organizing messages at your direction;</li>
                <li>sending messages that you explicitly approve;</li>
                <li>running email-triage schedules you configure; and</li>
                <li>maintaining security, reliability, and an inspectable record of agent work.</li>
              </ul>
              <p>
                Veetbot does not sell Google user data, use it for advertising,
                use it to determine creditworthiness, or use it to train a
                general-purpose AI model. It is not an email-warming, cold email,
                unsolicited bulk-send, or recipient-profiling service.
              </p>
            </section>

            <section>
              <h2>5. AI processing and sharing</h2>
              <p>
                To perform an email task, relevant message content and metadata
                may be sent to the AI model provider configured by the Veetbot
                operator. This processing is limited to producing the requested
                user-facing result, such as a summary, classification, draft,
                or proposed action. The hosted deployment uses the OpenAI and
                Anthropic APIs, depending on the selected model. Veetbot does
                not permit either provider, or any replacement provider, to use
                transferred Google user data to train or improve a
                general-purpose AI or machine-learning model. A self-hosted
                local model keeps that processing on the operator&apos;s host.
              </p>
              <p>
                Under their standard API data controls, each hosted provider
                may retain request and response content for abuse monitoring
                for up to 30 days, subject to limited safety or legal
                exceptions. See the{
                " "
                }<a href="https://platform.openai.com/docs/models/default-usage-policies-by-endpoint">
                  OpenAI API data controls
                </a>{
                " "
                }and the{
                " "
                }<a href="https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data">
                  Anthropic API retention policy
                </a>.
              </p>
              <p>
                Veetbot may otherwise disclose data only to service providers
                needed to operate the requested feature, to investigate a
                security incident, when you explicitly direct or consent to the
                disclosure, or when required by law. Human access is limited to
                those purposes and to support you explicitly request. Veetbot
                does not transfer Google user data to advertisers, data brokers,
                information resellers, or lending and eligibility services.
              </p>
            </section>

            <section>
              <h2>6. Storage and retention</h2>
              <p>
                OAuth client secrets and refresh tokens are stored in private
                operator-controlled credential files. Production storage that
                contains Google user data is encrypted at rest. Tokens and raw
                Google error responses are excluded from Veetbot tool results,
                events, and logs.
              </p>
              <p>
                Gmail content selected for a task, tool inputs and outputs,
                generated summaries and drafts, approval records, and related
                artifacts may be retained in Veetbot&apos;s session and run
                history until you delete the session or ask the deployment
                operator to delete it. This supports inspection, continuation,
                recovery, and auditing. Deletion removes the corresponding live
                records and artifacts; encrypted backup copies may remain for
                no more than 35 days before automatic expiration. Deleting mail
                in Gmail does not automatically delete a copy already retained
                in a Veetbot record.
              </p>
            </section>

            <section>
              <h2>7. Your choices and deletion</h2>
              <p>You may:</p>
              <ul>
                <li>decline the Gmail permissions and use Veetbot without email tools;</li>
                <li>
                  revoke Veetbot&apos;s Google access through your Google Account&apos;s
                  third-party connection settings;
                </li>
                <li>disable the Gmail integration and remove its credential files; and</li>
                <li>
                  delete available sessions or ask the deployment operator to
                  delete associated Veetbot records and artifacts.
                </li>
              </ul>
              <p>
                Revoking Google access stops future Gmail API access but does
                not automatically erase existing Veetbot records. A deletion
                request may be subject to short-lived backup retention and any
                legal obligation that requires preservation.
              </p>
            </section>

            <section>
              <h2>8. Security</h2>
              <p>
                Veetbot uses scoped credentials, access controls, encrypted
                transport, deterministic policy checks, approval requirements,
                bounded outputs, and secret-redaction controls. No system is
                perfectly secure, and operators remain responsible for securing
                their hosts, provider accounts, and backups.
              </p>
              <p>
                Gmail content is treated as untrusted external input. It cannot
                authorize an action, grant itself additional access, or bypass
                policy. Mailbox changes and sends require a separate explicit
                human approval after Veetbot displays the proposed action.
              </p>
            </section>

            <section className="limited-use">
              <h2>9. Google API Limited Use</h2>
              <p>
                Veetbot&apos;s use of information received from Google APIs will
                adhere to the{
                " "
                }<a href="https://developers.google.com/terms/api-services-user-data-policy">
                  Google API Services User Data Policy
                </a>, including the Limited Use requirements.
              </p>
            </section>

            <section>
              <h2>10. Changes and contact</h2>
              <p>
                This policy may change when Veetbot&apos;s data practices change.
                The effective date above will be updated, and material changes
                affecting Google data will be disclosed before the new use
                begins. For privacy or deletion requests, use the support
                address displayed on Veetbot&apos;s Google OAuth consent screen.
                For non-sensitive public questions, use the{
                " "
                }<a href="https://github.com/avitus/veetbot/issues">project issue tracker</a>.
              </p>
              <p>
                See also the <Link href="/tos">Veetbot Terms of Service</Link>.
              </p>
            </section>
          </div>
        </div>
      </article>
      <SiteFooter />
    </main>
  );
}
