# ADR-0357: Open generated HTML pages beside a Mac chat

- Status: Proposed
- Date: 2026-11-27
- Related: ADR-0122, ADR-0237, ADR-0330

The owner requested a browser window for iterating on webpages made in chat.
Use the existing authenticated artifact read to open an HTML attachment in a
separate macOS window. The native client gives WebKit only the downloaded
bytes, with an ephemeral data store, scripts disabled and navigation confined
to the document. The preview has no account token, browser profile, network
resources or native message handler. The server continues to serve attachments
as downloads. An arbitrary URL, directory or workspace path cannot open through
this flow.

Artifact IDs identify immutable versions. Start with the selected attachment
and follow new same-name HTML outputs in that conversation. Other files and
other chats do not replace the page. The filename is display metadata, never a
filesystem path. The inspector retains the source and the original download.
