"""Deterministic lexical grouping of chat conversations (Milestone 29).

This is the floor the model refines and the whole answer whenever the model
is unavailable: token overlap over a conversation's title and first-message
snippet, connected components at a similarity threshold, and a name derived
from the terms the members share. Identical input yields identical output.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.errors import FolderNameError
from agent_core.domain.folders import (
    FolderProposalDerivation,
    FolderProposalKind,
    folder_name_key,
    normalize_folder_name,
)
from agent_core.domain.memory import lexical_tokens
from agent_core.domain.messages import ModelUsage

MINIMUM_TERM_LENGTH = 3
MINIMUM_SHARED_TERMS = 2
NAME_TERMS = 2
STOPWORDS = frozenset(
    """
    about above after again all also and any are around because been before being
    between both but can cannot could did does doing done down during each either else
    even ever every few for from get gets getting give given got had has have having
    help here hers him his how however into its itself just keep know later least less
    let like made make many may maybe might more most much must need needs never next
    not now off often once one only other our ours out over own per please put quite
    rather really said same say says see seem seems she should since some something soon
    still such sure take than thank thanks that the their theirs them then there these
    they thing things think this those though through too under until upon use used
    using very want wants was way were what when where whether which while who whom
    whose why will with within without would yes yet you your yours
    """.split()  # noqa: SIM905 - a word list reads as prose, not as 165 literals
)


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    """One unfiled conversation as the grouper sees it."""

    session_id: UUID
    title: str
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class FolderSummary:
    """One existing folder with a bounded sample of its members' titles."""

    id: UUID
    name: str
    member_titles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GroupingInput:
    threads: tuple[ThreadSummary, ...]
    folders: tuple[FolderSummary, ...]
    threshold: int
    max_members: int
    similarity_threshold: float


@dataclass(frozen=True, slots=True)
class GroupCandidate:
    """A grouping a grouper suggests; the pass re-checks every rule before proposing."""

    kind: FolderProposalKind
    name: str | None
    target_folder_id: UUID | None
    member_session_ids: tuple[UUID, ...]
    rationale: str | None
    derivation: FolderProposalDerivation


@dataclass(frozen=True, slots=True)
class GroupingOutcome:
    """The candidates plus the content-free audit facts of how they were made."""

    candidates: tuple[GroupCandidate, ...]
    provider: str = "none"
    model: str = "none"
    usage: ModelUsage | None = None
    fallback_used: bool = False
    error_class: str | None = None


class ThreadGrouper(Protocol):
    async def group(self, grouping: GroupingInput, *, principal: Principal) -> GroupingOutcome: ...


def _terms(text: str) -> frozenset[str]:
    return frozenset(
        token
        for token in lexical_tokens(text)
        if len(token) >= MINIMUM_TERM_LENGTH and not token.isdigit() and token not in STOPWORDS
    )


def thread_terms(title: str, snippet: str = "") -> frozenset[str]:
    """The terms a conversation groups on: its title and snippet, minus noise."""

    return _terms(f"{title}\n{snippet}")


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def overlap(left: frozenset[str], right: frozenset[str]) -> float:
    smaller = min(len(left), len(right))
    return len(left & right) / smaller if smaller else 0.0


def _linked(
    left_all: frozenset[str],
    right_all: frozenset[str],
    left_title: frozenset[str],
    right_title: frozenset[str],
    threshold: float,
) -> bool:
    # The title is the strongest signal: two conversations that share only
    # snippet boilerplate never link, however similar their small talk.
    if not left_title & right_title:
        return False
    shared = left_all & right_all
    return len(shared) >= MINIMUM_SHARED_TERMS and jaccard(left_all, right_all) >= threshold


def _derive_name(title_terms: list[frozenset[str]]) -> str | None:
    frequency: Counter[str] = Counter()
    for terms in title_terms:
        frequency.update(terms)
    ranked = sorted(frequency.items(), key=lambda item: (-item[1], item[0]))
    words = [term.capitalize() for term, _count in ranked[:NAME_TERMS]]
    if not words:
        return None
    try:
        return normalize_folder_name(" ".join(words))
    except FolderNameError:
        return None


def lexical_groups(grouping: GroupingInput) -> list[GroupCandidate]:
    """Connected components at the similarity floor, then additions to folders."""

    threads = list(grouping.threads)
    order = {thread.session_id: index for index, thread in enumerate(threads)}
    all_terms = {t.session_id: thread_terms(t.title, t.snippet) for t in threads}
    title_terms = {t.session_id: _terms(t.title) for t in threads}
    parent: dict[UUID, UUID] = {t.session_id: t.session_id for t in threads}

    def find(item: UUID) -> UUID:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for index, left in enumerate(threads):
        for right in threads[index + 1 :]:
            if _linked(
                all_terms[left.session_id],
                all_terms[right.session_id],
                title_terms[left.session_id],
                title_terms[right.session_id],
                grouping.similarity_threshold,
            ):
                parent[find(left.session_id)] = find(right.session_id)

    components: dict[UUID, list[UUID]] = {}
    for thread in threads:
        components.setdefault(find(thread.session_id), []).append(thread.session_id)

    existing = {folder_name_key(folder.name): folder.id for folder in grouping.folders}
    new_folders: list[GroupCandidate] = []
    additions: dict[UUID, list[UUID]] = {}
    used: set[UUID] = set()
    for members in components.values():
        if len(members) < grouping.threshold:
            continue
        members = sorted(members, key=lambda item: order[item])[: grouping.max_members]
        name = _derive_name([title_terms[member] for member in members])
        if name is None:
            continue
        used.update(members)
        sorted_members = tuple(sorted(members, key=lambda item: item.int))
        target = existing.get(name.casefold())
        if target is not None:
            additions.setdefault(target, []).extend(sorted_members)
            continue
        new_folders.append(
            GroupCandidate(
                kind=FolderProposalKind.NEW_FOLDER,
                name=name,
                target_folder_id=None,
                member_session_ids=sorted_members,
                rationale=None,
                derivation=FolderProposalDerivation.LEXICAL,
            )
        )

    profiles = {
        folder.id: (
            _terms("\n".join((folder.name, *folder.member_titles))),
            _terms("\n".join((folder.name, *folder.member_titles))),
        )
        for folder in grouping.folders
    }
    for thread in threads:
        if thread.session_id in used:
            continue
        best: tuple[float, str, UUID] | None = None
        for folder in grouping.folders:
            profile_all, profile_title = profiles[folder.id]
            shared = all_terms[thread.session_id] & profile_all
            if not title_terms[thread.session_id] & profile_title:
                continue
            score = overlap(all_terms[thread.session_id], profile_all)
            if len(shared) < MINIMUM_SHARED_TERMS or score < grouping.similarity_threshold:
                continue
            key = (score, folder_name_key(folder.name), folder.id)
            if best is None or key > best:
                best = key
        if best is not None:
            additions.setdefault(best[2], []).append(thread.session_id)

    folder_keys = {folder.id: folder_name_key(folder.name) for folder in grouping.folders}
    addition_candidates = [
        GroupCandidate(
            kind=FolderProposalKind.ADD_TO_FOLDER,
            name=None,
            target_folder_id=folder_id,
            member_session_ids=tuple(
                sorted(set(members), key=lambda item: item.int)[: grouping.max_members]
            ),
            rationale=None,
            derivation=FolderProposalDerivation.LEXICAL,
        )
        for folder_id, members in additions.items()
    ]
    new_folders.sort(key=lambda c: (-len(c.member_session_ids), c.member_session_ids[0].int))
    addition_candidates.sort(
        key=lambda c: (folder_keys.get(c.target_folder_id or UUID(int=0), ""), c.member_session_ids)
    )
    return [*new_folders, *addition_candidates]


class LexicalGrouper:
    """The deterministic grouper; never calls a provider."""

    async def group(self, grouping: GroupingInput, *, principal: Principal) -> GroupingOutcome:
        del principal
        return GroupingOutcome(candidates=tuple(lexical_groups(grouping)))
