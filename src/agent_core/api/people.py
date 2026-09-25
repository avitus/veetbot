"""People routes with explicit scope, ceiling and retry boundaries."""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.exceptions import RequestValidationError
from pydantic import AwareDatetime

from agent_core.application.services import PeopleService
from agent_core.domain.agents import Principal
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleOperation, PeopleRelationshipFilter, Person
from agent_core.domain.people_imports import (
    PeopleImportCancel,
    PeopleImportRequest,
    PeopleImportView,
)
from agent_core.domain.people_public import (
    IdentityOperationView,
    MergeSuggestionPageView,
    MergeSuggestionView,
    OperationView,
    PeoplePageView,
    PeopleSectionPageView,
    PersonProfileView,
    PersonView,
)
from agent_core.domain.people_views import (
    CreatePerson,
    MergeSuggestionDetail,
    MergeSuggestionPage,
    PeopleCorrectionRequest,
    PeopleCorrectionResult,
    PeopleErasureView,
    PeopleEvidenceView,
    PeopleForgetRequest,
    PeopleIdentityRequest,
    PeoplePage,
    PeopleSectionPage,
    PeopleSectionQuery,
    PersonProfile,
    ResolveMergeSuggestion,
    UpdatePerson,
)
from agent_core.domain.views import Page


def private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


def _time_range(since: datetime | None, until: datetime | None) -> None:
    if since is not None and until is not None and since >= until:
        raise RequestValidationError([{"loc": ("query", "until"), "type": "value_error"}])


def _section_query(
    section: Literal["relationships", "history", "facts"],
) -> Callable[..., PeopleSectionQuery]:
    def query(
        as_of: AwareDatetime | None = None,
        known_at: AwareDatetime | None = None,
        since: AwareDatetime | None = None,
        until: AwareDatetime | None = None,
        channel: Literal["chat", "email", "sms"] | None = None,
        interaction_kind: Literal[
            "exchange",
            "meeting",
            "visit",
            "introduction",
            "trip",
            "milestone",
            "decision",
            "disagreement",
            "other",
        ]
        | None = None,
        unknown_time: Literal["include", "only", "exclude"] = "include",
        include_inactive: bool = False,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ) -> PeopleSectionQuery:
        _time_range(since, until)
        return PeopleSectionQuery(
            section=section,
            as_of=as_of,
            known_at=known_at,
            since=since,
            until=until,
            channel=channel,
            interaction_kind=interaction_kind,
            unknown_time=unknown_time,
            include_inactive=include_inactive,
            limit=limit,
            cursor=cursor,
        )

    return query


def people_router(service: PeopleService, secured: Callable[[str], object]) -> APIRouter:
    router = APIRouter(dependencies=[Depends(private_response)])

    @router.get(
        "/v1/people", response_model=PeoplePageView, openapi_extra={"required_scope": "people.read"}
    )
    async def people(
        authenticated: Annotated[Principal, secured("people.read")],
        ceiling: Sensitivity,
        text: Annotated[str | None, Query(max_length=512)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        as_of: AwareDatetime | None = None,
        # Repeat state to list several; People is active plus provisional (ADR-0121).
        state: Annotated[
            list[Literal["active", "provisional", "merged"]] | None, Query(max_length=3)
        ] = None,
        pinned: bool | None = None,
        sort: Literal["id", "recent"] = "id",
        relationship: PeopleRelationshipFilter | None = None,
        # Provisional, unpinned people with no confirmed or observed identifier.
        review: bool = False,
    ) -> PeoplePage:
        return await service.list(
            authenticated,
            ceiling=ceiling,
            text=text,
            limit=limit,
            cursor=cursor,
            as_of=as_of,
            states=state,
            pinned=pinned,
            sort=sort,
            relationship=relationship,
            review=review,
        )

    @router.post(
        "/v1/people", response_model=PersonView, openapi_extra={"required_scope": "people.write"}
    )
    async def create(
        body: CreatePerson,
        authenticated: Annotated[Principal, secured("people.write")],
        ceiling: Sensitivity,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ) -> Person:
        return await service.create(authenticated, body, key=idempotency_key, ceiling=ceiling)

    @router.post(
        "/v1/people/identity-operations",
        response_model=IdentityOperationView,
        openapi_extra={"required_scope": "people.write"},
    )
    async def identity_operation(
        body: PeopleIdentityRequest,
        authenticated: Annotated[Principal, secured("people.write")],
        ceiling: Sensitivity,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ) -> PeopleOperation:
        return await service.identity_operation(
            authenticated, body, key=idempotency_key, ceiling=ceiling
        )

    # Static People paths are registered before `/v1/people/{person_id}`.
    @router.get(
        "/v1/people/merge-suggestions",
        response_model=MergeSuggestionPageView,
        openapi_extra={"required_scope": "people.read"},
    )
    async def merge_suggestions(
        authenticated: Annotated[Principal, secured("people.read")],
        ceiling: Sensitivity,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ) -> MergeSuggestionPage:
        """Open possible duplicates the owner can merge or keep apart (ADR-0125)."""
        return await service.merge_suggestions(
            authenticated, ceiling=ceiling, limit=limit, cursor=cursor
        )

    @router.post(
        "/v1/people/merge-suggestions/{suggestion_id}",
        response_model=MergeSuggestionView,
        openapi_extra={"required_scope": "people.write"},
    )
    async def resolve_merge_suggestion(
        suggestion_id: UUID,
        body: ResolveMergeSuggestion,
        authenticated: Annotated[Principal, secured("people.write")],
        ceiling: Sensitivity,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ) -> MergeSuggestionDetail:
        """Merge the pair, or keep them apart so they are never suggested again."""
        return await service.resolve_merge_suggestion(
            authenticated, suggestion_id, body, key=idempotency_key, ceiling=ceiling
        )

    @router.get(
        "/v1/people/operations/{operation_id}",
        response_model=OperationView,
        openapi_extra={"required_scope": "people.read"},
    )
    async def operation(
        operation_id: UUID,
        authenticated: Annotated[Principal, secured("people.read")],
        ceiling: Sensitivity,
    ) -> PeopleOperation | PeopleErasureView:
        return await service.operation(authenticated, operation_id, ceiling=ceiling)

    @router.get(
        "/v1/people/imports",
        response_model=Page[PeopleImportView],
        openapi_extra={"required_scope": "people.read"},
    )
    async def list_imports(
        authenticated: Annotated[Principal, secured("people.read")],
        ceiling: Sensitivity,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ) -> Page[PeopleImportView]:
        return await service.list_imports(
            authenticated, ceiling=ceiling, limit=limit, cursor=cursor
        )

    @router.post(
        "/v1/people/imports",
        response_model=PeopleImportView,
        openapi_extra={"required_scope": "people.write"},
    )
    async def create_import(
        body: PeopleImportRequest,
        authenticated: Annotated[Principal, secured("people.write")],
        ceiling: Sensitivity,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ) -> PeopleImportView:
        return await service.create_import(
            authenticated, body, key=idempotency_key, ceiling=ceiling
        )

    @router.get(
        "/v1/people/imports/{job_id}",
        response_model=PeopleImportView,
        openapi_extra={"required_scope": "people.read"},
    )
    async def get_import(
        job_id: UUID,
        authenticated: Annotated[Principal, secured("people.read")],
        ceiling: Sensitivity,
    ) -> PeopleImportView:
        return await service.get_import(authenticated, job_id, ceiling=ceiling)

    @router.post(
        "/v1/people/imports/{job_id}/cancel",
        response_model=PeopleImportView,
        openapi_extra={"required_scope": "people.write"},
    )
    async def cancel_import(
        job_id: UUID,
        body: PeopleImportCancel,
        authenticated: Annotated[Principal, secured("people.write")],
        ceiling: Sensitivity,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ) -> PeopleImportView:
        return await service.cancel_import(
            authenticated, job_id, body, key=idempotency_key, ceiling=ceiling
        )

    @router.get(
        "/v1/people/{person_id}",
        response_model=PersonProfileView,
        openapi_extra={"required_scope": "people.read"},
    )
    async def person(
        person_id: UUID,
        authenticated: Annotated[Principal, secured("people.read")],
        ceiling: Sensitivity,
    ) -> PersonProfile:
        return await service.get(authenticated, person_id, ceiling=ceiling)

    @router.patch(
        "/v1/people/{person_id}",
        response_model=PersonView,
        openapi_extra={"required_scope": "people.write"},
    )
    async def update(
        person_id: UUID,
        body: UpdatePerson,
        authenticated: Annotated[Principal, secured("people.write")],
        ceiling: Sensitivity,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ) -> Person:
        return await service.update(
            authenticated, person_id, body, key=idempotency_key, ceiling=ceiling
        )

    @router.get(
        "/v1/people/{person_id}/relationships",
        response_model=PeopleSectionPageView,
        openapi_extra={"required_scope": "people.read"},
    )
    async def relationships(
        person_id: UUID,
        authenticated: Annotated[Principal, secured("people.read")],
        ceiling: Sensitivity,
        query: Annotated[PeopleSectionQuery, Depends(_section_query("relationships"))],
    ) -> PeopleSectionPage:
        return await service.section(authenticated, person_id, query, ceiling=ceiling)

    @router.get(
        "/v1/people/{person_id}/history",
        response_model=PeopleSectionPageView,
        openapi_extra={"required_scope": "people.read"},
    )
    async def history(
        person_id: UUID,
        authenticated: Annotated[Principal, secured("people.read")],
        ceiling: Sensitivity,
        query: Annotated[PeopleSectionQuery, Depends(_section_query("history"))],
    ) -> PeopleSectionPage:
        return await service.section(authenticated, person_id, query, ceiling=ceiling)

    @router.get(
        "/v1/people/{person_id}/facts",
        response_model=PeopleSectionPageView,
        openapi_extra={"required_scope": "people.read"},
    )
    async def facts(
        person_id: UUID,
        authenticated: Annotated[Principal, secured("people.read")],
        ceiling: Sensitivity,
        query: Annotated[PeopleSectionQuery, Depends(_section_query("facts"))],
    ) -> PeopleSectionPage:
        return await service.section(authenticated, person_id, query, ceiling=ceiling)

    @router.get(
        "/v1/people/{person_id}/identity-evidence",
        response_model=PeopleSectionPageView,
        openapi_extra={"required_scope": "people.read"},
    )
    async def identity_evidence(
        person_id: UUID,
        authenticated: Annotated[Principal, secured("people.read")],
        ceiling: Sensitivity,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
    ) -> PeopleSectionPage:
        return await service.section(
            authenticated,
            person_id,
            PeopleSectionQuery(section="identity-evidence", limit=limit, cursor=cursor),
            ceiling=ceiling,
        )

    @router.get(
        "/v1/people/{person_id}/evidence/{reference}",
        openapi_extra={"required_scope": "people.read"},
    )
    async def evidence(
        person_id: UUID,
        reference: UUID,
        authenticated: Annotated[Principal, secured("people.read")],
        ceiling: Sensitivity,
    ) -> PeopleEvidenceView:
        return await service.evidence(authenticated, person_id, reference, ceiling=ceiling)

    @router.post(
        "/v1/people/{person_id}/corrections", openapi_extra={"required_scope": "people.write"}
    )
    async def correct(
        person_id: UUID,
        body: PeopleCorrectionRequest,
        authenticated: Annotated[Principal, secured("people.write")],
        ceiling: Sensitivity,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ) -> PeopleCorrectionResult:
        return await service.correct(
            authenticated, person_id, body, key=idempotency_key, ceiling=ceiling
        )

    @router.post("/v1/people/{person_id}/forget", openapi_extra={"required_scope": "people.write"})
    async def forget(
        person_id: UUID,
        body: PeopleForgetRequest,
        authenticated: Annotated[Principal, secured("people.write")],
        ceiling: Sensitivity,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ) -> PeopleErasureView:
        return await service.forget(
            authenticated, person_id, body, key=idempotency_key, ceiling=ceiling
        )

    return router
