"""People proposals attached to the existing metered email assessment."""

from typing import Any

from pydantic import Field, model_validator

from agent_core.domain.email import EmailAssessment
from agent_core.domain.email_semantics import EmailSemanticFact
from agent_core.domain.people_extraction import OrganizationEvidence, PeopleClaim, PersonEvidence


class EmailPeopleFact(EmailSemanticFact):
    people: PeopleClaim | None

    @model_validator(mode="after")
    def local_quote_evidence(self) -> "EmailPeopleFact":
        if self.people is not None:
            mentions: list[PersonEvidence | OrganizationEvidence] = [
                *self.people.mentions,
                *self.people.organizations,
            ]
            for mention in mentions:
                if (
                    mention.source_event_id != 1
                    or self.quote[mention.start : mention.end] != mention.text
                ):
                    raise ValueError("email People evidence must address the exact local quote")
            if self.people.commitment is not None and self.people.commitment.source_event_id != 1:
                raise ValueError("email commitment state must address the local quote")
        return self


class EmailPeopleAssessment(EmailAssessment):
    people_facts: list[EmailPeopleFact] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def shared_people_budget(self) -> "EmailPeopleAssessment":
        if (
            sum(
                len(fact.people.mentions) + len(fact.people.organizations)
                for fact in self.people_facts
                if fact.people is not None
            )
            > 64
        ):
            raise ValueError("email assessment exceeds 64 People mentions")
        return self


def email_people_schema() -> dict[str, Any]:
    """Strict assessment schema with quote-local evidence identifiers."""
    schema = EmailPeopleAssessment.model_json_schema()

    def require_properties(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object":
                properties = value.get("properties", {})
                value["required"] = list(properties)
                if "source_event_id" in properties:
                    properties["source_event_id"] = {"type": "integer", "const": 1}
            for child in value.values():
                require_properties(child)
        elif isinstance(value, list):
            for child in value:
                require_properties(child)

    require_properties(schema)
    return schema
