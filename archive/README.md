# Archive

This directory holds the source Word documents from which the canonical Markdown
documentation was generated. **Do not edit files in this directory.**

| Field | Value |
| --- | --- |
| Canonical source filename | `Modular_General_Purpose_AI_Agent_Engineering_Plan.docx` |
| Archived path | `archive/Modular_General_Purpose_AI_Agent_Engineering_Plan.docx` |
| SHA-256 | `993235588b46e264eb5269709b38503dd38c5e70295947d367318d51e8f958d6` |
| Conversion date (UTC) | `2026-07-20T23:12:41Z` |
| Canonical converted path | `docs/plan/engineering-plan.md` |

The Word documents are **archival only**. As of the conversion date above, the
Markdown at `docs/plan/engineering-plan.md` is the canonical, normative source of
the engineering plan. Future changes must be made to the canonical Markdown and
YAML sources, never to these archived `.docx` files.

## Version history

The engineering plan evolved through several revisions before conversion. The
full Word history is preserved under `archive/versions/`. The canonical archived
document above is a byte-for-byte copy of **v2.3** (the latest revision), which is
what was converted to Markdown.

| Version | File (under `archive/versions/`) | Notes |
| --- | --- | --- |
| v1.0 | `Modular General Purpose AI Agent Engineering Plan.docx` | Original design (26 numbered sections). |
| v2.0 | `Modular General-Purpose AI Agent Engineering Plan v2.0.docx` | Production-readiness review. |
| v2.1 | `Modular General-Purpose AI Agent Engineering Plan v2.1.docx` | Copy-ready streaming maps; multi-device shared core. |
| v2.2 | `Modular General-Purpose AI Agent Engineering Plan v2.2.docx` | Additions informed by a review of Nous Research's Hermes Agent. |
| v2.3 | `Modular General-Purpose AI Agent Engineering Plan v2.3.docx` | Build-sequencing pass. Canonical source for the conversion (31 numbered sections). |

## Conversion method

Converted with Pandoc (`docx` → `gfm`), followed by a deterministic cleanup pass
(single level-one title, fenced code blocks with language hints, normalized
tables, and removal of the static Word table of contents and title-page
artifacts). No substantive requirement was rewritten during conversion.

## One documented correction

The conversion relocated three security controls — non-bypassable hardline rules,
tiered credential scrubbing with fail-closed env passthrough, and default-deny
pairing for untrusted inbound surfaces — from the document's "Revision summary"
list into their correct home under Section 22, "Security baseline". The three
controls had been mis-placed by an earlier automated edit of the source document.
No requirement text was changed; only placement was corrected. The archived
`.docx` files retain the original placement; the canonical Markdown carries the
corrected placement. See `docs/changelog.md`.
