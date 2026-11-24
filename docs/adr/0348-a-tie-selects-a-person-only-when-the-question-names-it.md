# ADR-0348: A tie selects a person only when the question names it

- Status: Accepted (built on a local branch on 2026-11-23 while diagnosing the
  owner question set; the owner accepted it the same day, answering "Accept
  the new rule for choosing who a question is about?" with "Yes, accept it")
- Date: 2026-11-23
- Amends: ADR-0208 (which relationship evidence selects a person), ADR-0310
  (its label requirement no longer applies to focused searches alone)
- Related: ADR-0121, ADR-0125, ADR-0264, ADR-0309, ADR-0338, ADR-0346
- Detailed design: `docs/plan/people-and-relationships.md`
- Scope: how People context selects a focal person for relevance recall. No
  identity merge, no ranking, packing, schema, retrieval version, milestone or
  gate change.

## Context

When recall decides that a question is about a person, two things follow. That
person's context is added, and every memory assigned to any other person is
excluded (the identity rule). The second effect is right when the person is
right. When the person is wrong, or when recall abstains because it found
several, the question does worse than one that mentions nobody: ordinary recall
would have ranked those memories normally.

The owner question set showed three ways the wrong person is chosen. They were
read from a clone of production taken on 2026-11-23, by ids and stages only.

1. **A relative known only by a role.** Formation keeps an unnamed relative as
   a provisional record displayed by the owner's own words, such as "mom"
   (ADR-0121). Its only alias is an unconfirmed role alias. Name matching
   already refuses to select anyone through an unconfirmed role alias, but it
   matched the same word again as the record's display name. A question using
   that word selected the role-only record, and the facts linked to the named
   record of the same relative were excluded. Three questions lost one fact
   this way.
2. **A shared word.** With no name match, discovery (ADR-0208) took any
   relationship memory sharing one word with the question as evidence for the
   people whose ties it supports. A question about one relative selected
   another, because that other relative's relationship memory shared an
   ordinary word with it.
3. **Several loose matches.** When such evidence reached two or more people,
   discovery abstained and every person-linked fact was excluded. A general
   family question lost a required name, and a question about two relatives
   lost both their facts.

ADR-0310 had already met the second case for focused deliberate searches:
discovery there selects a person only when the question contains the whole
stored label of a tie. Automatic recall and older search versions kept the
broad overlap.

Merging the role-only records into their named counterparts was measured on
the clone and changed nothing (99 of 114 before and after): the shared-word
choice then selected a different relative. Merging also stays an owner
decision. Names and unnamed relatives never merge by themselves (gate P01,
ADR-0125).

## Decision

1. **A role-only display name is a role, not a name.** In name matching, a
   person whose display name equals one of that person's own role aliases that
   the owner has not confirmed is not selected through the display name. An
   owner-confirmed role alias still selects, as before.
2. **Discovery requires the named label everywhere.** For every relevance
   recall, automatic and deliberate, every search version: relationship
   memories that share a word with the question still propose people, bounded
   as before. A proposed person is selected only when a visible, resolved,
   direct edge between that person and the owner, supported by one of those
   memories, carries a label whose whole lexemes occur in the question. The
   label is the edge's qualifier, or its predicate when there is no qualifier
   and the predicate is not the generic one. This is ADR-0310's rule, now
   applied before people are counted.
3. **Outcomes.** Nobody named: no person is selected and ordinary recall runs
   untouched, hiding nothing. Exactly one person named: that person is
   selected, and the memories behind the named edges are recorded as the
   selection evidence. Two or more named: recall abstains from person context
   and excludes person-linked memories, as ADR-0208 requires. A proposal set
   larger than twenty people also abstains; an edge read larger than one
   hundred rows leaves ordinary recall, as ADR-0310 decided.
4. **Nothing else moves.** Explicit session selections, name matches of named
   people, name ambiguity, owner overviews, the identity rule, hard filters,
   budgets and rendering are unchanged. No role vocabulary is added and no
   identity is merged, created or inferred.

## Consequences

- A question that uses a role word the directory does not store as a label
  ("my wife" where the tie is stored as a spouse with no qualifier) no longer
  selects that person. It gets ordinary recall: no profile line and no boost
  for that person's linked facts, and nothing excluded. Before, the outcome
  depended on which relationship memory happened to share a word.
- The role-only records remain in the directory. Whether to merge them is
  still the owner's choice in Needs review; this change does not depend on it.
  On the clone, merging in addition made one held-out fact rank lower; the
  cause was not examined.
- Discovery makes one bounded edge read on every relevance recall that
  proposes people, where it previously did so only for focused searches. No
  model or encoder call is added. Timing was not measured separately.
- Selection evidence recorded for retained deliberate results is now the
  memories behind the named edges for every query, as it already was for
  focused ones.

## Verification

Red first, in the Linux container: an unfocused question sharing only a topic
word with a stored role selected the person; a question using a role-only
record's word excluded the named relative's fact; a question sharing a word
with two relatives' ties excluded a linked fact. The same cases run against
the in-memory and PostgreSQL adapters. The existing ADR-0208 cases still pass
unchanged, including the one where two people hold the named label and recall
must abstain.

Owner question set on the clone, 114 questions, both paths, no model calls,
semantic recall on every path, counted under ADR-0346:

| Change | Passed | Development | Held-out |
| --- | --- | --- | --- |
| None (baseline) | 99 | 52 | 47 |
| Merge the two mergeable role-only records only | 99 | 52 | 47 |
| Several loose matches fall back to ordinary recall | 101 | 53 | 48 |
| Several loose matches select up to three people | 100 | 52 | 48 |
| This decision | 103 | 54 | 49 |

No question that passed before fails after. The four gained questions are two
about one relative known by a role word, the general family question, and the
question about two relatives. One further fact about that relative becomes
eligible but ranks far below the list; it is a weak-link miss, not an identity
one.

## Alternatives considered

- **Merge role-only records automatically.** Refused by gate P01 for unnamed
  relatives, and measured to gain nothing by itself.
- **Fall back to ordinary recall whenever discovery is ambiguous.** Gains two
  questions, but it relaxes the accepted rule that an ambiguous role must not
  combine people, and it leaves the role-only and shared-word cases.
- **Select every person who holds the tie, up to three.** Gains one question
  and relaxes the same rule.
- **Map role words to ties with the kinship table.** It would let "my wife"
  select a spouse tie, but mother and father share one kinship key and every
  role-only record would again collide with its named counterpart. ADR-0208
  and ADR-0310 also rule out a role vocabulary.
