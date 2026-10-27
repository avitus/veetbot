# ADR-0259: Owner places and family ties reach recall

- Status: Proposed (the owner asked for these repairs on 2026-10-27, and the
  same day for the word-ending folding and the probe's name check)
- Date: 2026-10-27
- Related: ADR-0100, ADR-0121, ADR-0125, ADR-0232, ADR-0238, ADR-0247, ADR-0249
- Detailed design: `docs/plan/memory-retrieval-and-ranking.md`,
  `docs/plan/people-and-relationships.md`

## Context

The owner recall probes run on a production copy on 2026-10-26 missed three
facts the owner had stated:

1. **Home location.** "Maintain a gardening calendar based on what you know
   about our garden" never made the owner's residence a candidate on the
   automatic path. The question shares no word with "User lives in …", and
   owner-question detection recognises "my" and question forms, not "our
   garden". The model then asked which city the garden is in.
2. **Wife.** The owner wrote "Erin is my wife. We married in 2005. Kyrri is
   my second daughter." on 2026-09-29. The chat saved "Andy Vitus says Erin is
   his wife" linked to Erin's People record, but the person-linked save writes
   only a memory link, never a relationship edge. Family overviews read edges,
   so the owner's wife was absent from every one.
3. **Daughter.** Overview pages hold three people (ADR-0238) taken in
   identifier order, and automatic recall renders only the first page. One
   daughter fell to page two behind two parent records, one of them a
   provisional duplicate.

A fourth miss, the website-learned board seats, ranked 53rd on the automatic
path. "Which company boards do I sit on?" shared only "boards" with "He sits on
the boards of …", and memories sharing only "company" tied it and won on
recency. The private family probe also misreported: it named three memory
identifiers, while an overview delivers family as People lines.

## Decision

1. **Residence anchor, `+home-location@1`.** A task naming the owner's own
   household place with "my" or "our" (home, house, garden, yard, lawn,
   property, apartment, neighbourhood, town, city) anchors the owner's direct
   residence statements: `fact` or `user_model_attr`, direct, asserted, a
   subject that is not person-scoped, and a statement that says the user lives
   or resides somewhere, or gives a home (mailing) address. The match weight
   is 0.5, as for possessions; both stores rank anchored rows ahead of the
   candidate cap; PostgreSQL uses the same expression as a case-insensitive
   regular expression. Deliberate `memory.search` accepts keyword queries
   naming the user or owner with home, house, residence, address, lives or
   location, in
   relevance mode only. Queries that use the anchor append `+home-location@1`
   to their retrieval policy; other queries keep theirs. Hard filters,
   lifecycle weights, the attention cap and item/token budgets still apply,
   and the admitted semantic path clears the anchor as it clears possessions.

2. **Owner kinship from the owner's own words, `owner-kinship@1`.** A
   person-linked `relationship` save whose owner message states the tie in a
   plain present-tense clause ("Erin is my wife", "my mother, Cheryl, …",
   "Kyrri and Riv are my daughters") also records the owner relationship edge:
   spouse, partner, parent (in either direction) or sibling, supported by that
   message. The tie is read from the owner's message for the name that
   identified the person, never from the model's paraphrase, and the saved
   statement must name the same tie. A negation, "ex-", "late", "former", a
   past tense, a possessive chain ("my wife's friend Erin") or two different
   ties to one name record nothing. An existing current edge of the same kind
   is not duplicated, and an erased edge is not revived. Identity still comes
   only from the owner-selected person reference (ADR-0121); no name is
   matched to a person.

3. **Repair for earlier saves.** `agent people link-kinship --owner
   TENANT/PRINCIPAL [--confirm]` applies rule 2 to live, direct,
   owner-confirmed relationship memories already linked to People, reading
   each memory's own cited owner message. A linked person later merged
   contributes its edge to the surviving identity. Without `--confirm` it
   writes nothing; the report holds counts only.

4. **Closest family first, `+owner-overview@2`.** Owner overview pages order
   their focal people by the owner relationship: partner or spouse, children,
   parents, siblings, then other relatives, ties broken by identifier. Pages
   still hold three people, and `people_after` continues after the last
   person shown in that order. One overview reads at most 50 people per
   relationship group and 100 relationship rows per person; a group holding
   more says so on the page and points to asking about a person by name,
   so the page never looks complete when it is not.

5. **Word-ending folding, `retrieval@8`.** Relevance matching folds a plural
   or third-person "-s" off a plain ASCII query word: "boards" also matches
   "board", "sit" matches "sits", "companies" matches "company", "classes"
   matches "class". A word ending "ss", "us" or "is" keeps its "s", and a word
   of three letters or fewer is not shortened. The query is expanded into the
   spellings that fold to the same stem and stored text is never folded, so
   PostgreSQL matches the same spellings as whole lexemes of its unfolded
   `simple` vector in candidate selection, arm order and the lexical score.
   Legacy (`recent`) mode keeps whole-lexeme matching, so the base label
   `retrieval@4` and the frozen benchmark do not move; the live selector moves
   `retrieval@7 -> retrieval@8`.

6. **Probes check delivered names.** A probe may list `delivered_must_contain`:
   words or names the delivered recall must render as whole words. The private
   family probe checks the family's names rather than memory identifiers an
   overview never delivers. The report holds one flag per entry, never the
   text, and each entry counts as required evidence in the summary.

## Measurement

On the 402 synthetic seeds of the general-recall diagnostic driver (1,000
noise statements, relevance mode), folding raised rendered recall at 20 from
0.688 to 0.723 on the automatic path and from 0.688 to 0.725 on the
deliberate path. No family fell; mean rendered tokens rose from 310 to 338.
Legacy mode and the deterministic M16 benchmark are unchanged
(`needed_recalled` 58/60, `noise_total` 43). The learned board-seat claim now
matches two of the question's three words, ahead of the one-word flood.

## Consequences

- The garden task reaches "User lives in …" on the automatic path; the
  affirmed mailing address written with the owner's name ("Andy Vitus's home
  mailing address is …") is reached through its "home mailing address" words.
- The production repair run is an owner decision; it adds the spouse edge
  behind the 2026-09-29 save and any other tie a linked save already states.
- Frozen M16 and M33 controls, the ADR-0220 held-out and the base label
  `retrieval@4` are untouched: the anchor and the folding apply only in
  relevance mode, and the benchmark's metrics and probe rows are unchanged.
- Production recall traces read `retrieval@8` once this ships. Folding lets a
  few more one-word matches into the candidate set; the ranking arms, budgets
  and caps are unchanged.
