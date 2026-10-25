"""ADR-0251: an unconnected chat offers to connect a website the owner names."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.browser import WEBSITE_OFFER_STATE_KEY, BrowserProfileStatus
from agent_core.domain.errors import NotFoundError
from agent_core.domain.events import NewEvent
from agent_core.domain.sessions import SESSION_BROWSER_PROFILE_METADATA_KEY, Session
from agent_core.domain.views import WebsiteOfferProfileView, WebsiteOfferView
from agent_core.ports.persistence import RepositoryUnitOfWork

_WORD = re.compile(r"[a-z0-9]+")
# The public suffix list stays inside the isolated browser service (ADR-0128),
# so the registrable domain is approximated: a match only chooses a card.
_SECOND_LEVEL_LABELS = frozenset({"ac", "co", "com", "edu", "gov", "net", "org"})

MAXIMUM_OFFERED_WEBSITES = 3
_MAXIMUM_JOINED_WORDS = 3
# A domain label shorter than this matches only by host or registrable domain,
# so the letter x never names x.com.
_MINIMUM_NAME_LABEL = 4
_MINIMUM_HOST_LABEL = 7
_MINIMUM_PREFIX_WORD = 5


@dataclass(frozen=True, slots=True)
class OwnedWebsite:
    profile_id: UUID
    origins: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Names:
    literals: tuple[str, ...]
    domain_label: str | None
    host_label: str | None


def _registrable_domain(host: str) -> str | None:
    labels = host.split(".")
    if len(labels) < 2 or all(label.isdigit() for label in labels):
        return None
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL_LABELS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _names(origin: str) -> _Names:
    host = (urlsplit(origin).hostname or "").lower()
    if not host:
        return _Names((), None, None)
    domain = _registrable_domain(host)
    literals = (host,) if domain is None or domain == host else (host, domain)
    label = None if domain is None else domain.split(".")[0].replace("-", "")
    first = host.split(".")[0].replace("-", "")
    return _Names(
        literals=literals,
        domain_label=label if label is not None and len(label) >= _MINIMUM_NAME_LABEL else None,
        host_label=(
            first
            if domain is not None
            and host != domain
            and first != "www"
            and len(first) >= _MINIMUM_HOST_LABEL
            else None
        ),
    )


def _strength(text: str, words: Sequence[str], names: _Names) -> int:
    """The length of the longest text that names the website, or zero."""

    best = 0
    for literal in names.literals:
        if re.search(rf"(?<![a-z0-9.-]){re.escape(literal)}(?![a-z0-9-])", text):
            best = max(best, len(literal))
    labels = {label for label in (names.domain_label, names.host_label) if label}
    for start in range(len(words)):
        joined = ""
        for word in words[start : start + _MAXIMUM_JOINED_WORDS]:
            joined += word
            if joined in labels:
                best = max(best, len(joined))
    label = names.domain_label
    if label is not None and len(label) >= _MINIMUM_PREFIX_WORD:
        for word in words:
            if len(word) >= _MINIMUM_PREFIX_WORD and label.startswith(word):
                best = max(best, len(word))
    return best


def match_websites(
    text: str,
    websites: Sequence[OwnedWebsite],
    *,
    limit: int = MAXIMUM_OFFERED_WEBSITES,
) -> tuple[UUID, ...]:
    """Owned websites the owner's own text names, longest matched text first."""

    lowered = text.casefold()
    words = _WORD.findall(lowered)
    scored: list[tuple[int, int, UUID]] = []
    for index, website in enumerate(websites):
        strength = max(
            (_strength(lowered, words, _names(origin)) for origin in website.origins),
            default=0,
        )
        if strength:
            scored.append((-strength, index, website.profile_id))
    return tuple(profile_id for _, _, profile_id in sorted(scored)[:limit])


OFFERED_EVENT = "session.browser.offered"
# A connection can select these (bind_chat_website); the card signs in first
# when the profile is not Ready.
_OFFERABLE = frozenset(
    {
        BrowserProfileStatus.READY,
        BrowserProfileStatus.AUTHENTICATION_REQUIRED,
        BrowserProfileStatus.NEEDS_USER,
    }
)
_PROFILE_PAGE = 65


@dataclass(frozen=True, slots=True)
class WebsiteOffer:
    """What admission tells the model and, when websites matched, the owner."""

    websites: tuple[OwnedWebsite, ...]
    connected_origin: str | None


async def _connected_origin(
    uow: RepositoryUnitOfWork, principal: Principal, session: Session
) -> str | None:
    selected = session.metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY)
    if not isinstance(selected, str) or not selected:
        return None
    try:
        current = await uow.browser_profiles.get(UUID(selected), principal)
    except (NotFoundError, ValueError):
        return None
    return current.allowed_origins[0] if current.allowed_origins else None


async def prepare_website_offer(
    uow: RepositoryUnitOfWork, principal: Principal, session: Session, text: str
) -> WebsiteOffer | None:
    """Match owner text before admission; a match never connects the chat."""

    agent = await uow.agents.get_version(session.agent_id, session.agent_version)
    if (
        "browser.navigate" not in agent.enabled_tools
        or "browser.profile.read" not in principal.scopes
    ):
        return None
    selected = session.metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY)
    connected_origin = await _connected_origin(uow, principal, session)
    owned = [
        OwnedWebsite(profile.id, profile.allowed_origins)
        for profile in await uow.browser_profiles.list(principal, limit=_PROFILE_PAGE)
        if profile.status in _OFFERABLE and str(profile.id) != selected
    ]
    by_id = {website.profile_id: website for website in owned}
    websites = tuple(by_id[profile_id] for profile_id in match_websites(text, owned))
    if connected_origin is not None and not websites:
        return None
    return WebsiteOffer(websites=websites, connected_origin=connected_origin)


async def record_website_offer(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    run_id: UUID,
    session_id: UUID,
    offer: WebsiteOffer,
    offer_id: UUID,
) -> None:
    """Record the offer for the owner and the source run's runtime row."""

    if offer.websites:
        source = await uow.events.latest_before(
            session_id, (1 << 63) - 1, "user.message.created", principal, run_id=run_id
        )
        assert source is not None
        await uow.events.append(
            NewEvent(
                session_id=session_id,
                run_id=run_id,
                event_type=OFFERED_EVENT,
                actor_type="application",
                actor_id=principal.principal_id,
                payload={
                    "offer_id": str(offer_id),
                    "source_sequence": source.sequence,
                    "profiles": [
                        {"profile_id": str(website.profile_id), "origins": list(website.origins)}
                        for website in offer.websites
                    ],
                    "connected_origin": offer.connected_origin,
                },
            )
        )
    pending = await uow.checkpoints.latest(run_id)
    assert pending is not None
    pending.version += 1
    pending.working_state[WEBSITE_OFFER_STATE_KEY] = {
        "offered": list(
            dict.fromkeys(origin for website in offer.websites for origin in website.origins)
        ),
        "connected": offer.connected_origin,
    }
    await uow.checkpoints.write(run_id, pending, full=True)


OFFER_LIFETIME = timedelta(hours=24)
# The continuation seed's origin marker and derivation namespace.
CONTINUATION_KIND = "browser_offer"
_END = (1 << 63) - 1


async def open_website_offer(
    uow: RepositoryUnitOfWork, principal: Principal, session: Session, now: datetime
) -> WebsiteOfferView | None:
    """The chat's latest offer while nothing has closed it (ADR-0251 decision 4).

    A later message, any connection, 24 hours, or the loss of every offered
    profile closes it.
    """

    offered = await uow.events.latest_before(session.id, _END, OFFERED_EVENT, principal)
    if offered is None:
        return None
    expires_at = offered.created_at + OFFER_LIFETIME
    if now >= expires_at:
        return None
    for closing in ("user.message.created", "session.browser.connected"):
        later = await uow.events.latest_before(session.id, _END, closing, principal)
        if later is not None and later.sequence > offered.sequence:
            return None
    selected = session.metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY)
    websites: list[WebsiteOfferProfileView] = []
    for item in offered.payload.get("profiles", ()):
        try:
            profile = await uow.browser_profiles.get(UUID(str(item["profile_id"])), principal)
        except (KeyError, TypeError, ValueError, NotFoundError):
            continue
        if profile.status in _OFFERABLE and str(profile.id) != selected and profile.allowed_origins:
            websites.append(
                WebsiteOfferProfileView(
                    profile_id=profile.id,
                    origins=list(profile.allowed_origins),
                    status=profile.status,
                )
            )
    if not websites:
        return None
    return WebsiteOfferView(
        offer_id=UUID(str(offered.payload["offer_id"])),
        source_sequence=int(offered.payload["source_sequence"]),
        connected_origin=await _connected_origin(uow, principal, session),
        websites=websites,
        expires_at=expires_at,
    )


def continuation_text(origin: str) -> str:
    """The owner's tap in words: it names the website and nothing the owner said."""

    host = urlsplit(origin).hostname or origin
    return f"I connected {host} to this chat. Please continue my earlier request."
