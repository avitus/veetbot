"""The exact private proposal covered by one widget installation approval."""

import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_core.domain.argument_views import approval_argument_view
from agent_core.domain.cockpit import WIDGET_SLUG_PATTERN
from agent_core.domain.jobs import JobAccess, JobManifest


class InstallationJob(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str = Field(pattern=WIDGET_SLUG_PATTERN, max_length=48)
    source: str = Field(min_length=1, max_length=65536)
    manifest: JobManifest
    access: JobAccess
    expected_revision: UUID | None = None
    every_minutes: int = Field(ge=1, le=1440)


class WidgetInstallation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str = Field(pattern=WIDGET_SLUG_PATTERN, max_length=48)
    files: dict[str, str] = Field(min_length=1, max_length=32)
    expected_revision: UUID | None = None
    job: InstallationJob | None = None

    def summary(self) -> dict[str, Any]:
        """Show every proposed effect without putting source code on the approval card."""
        body: dict[str, Any] = {
            "widget": self.slug,
            "operation": "Create" if self.expected_revision is None else "Update",
            "public_code": "Publish widget source to cockpit/widgets",
            "expected_widget_revision": str(self.expected_revision)
            if self.expected_revision
            else None,
        }
        if self.job is not None:
            body.update(
                {
                    "public_code": (
                        "Publish widget source to cockpit/widgets and Python source to cockpit/jobs"
                    ),
                    "collector": self.job.slug,
                    "expected_job_revision": str(self.job.expected_revision)
                    if self.job.expected_revision
                    else None,
                    "private_access": self.job.model_dump(mode="json")["access"],
                    "refresh": (
                        f"Every {self.job.every_minutes} minutes, "
                        "starting at the next interval boundary"
                    ),
                }
            )
        return body

    def headline(self) -> str:
        """Render bounded, redacted values in fixed copy visible above the approval buttons."""
        lines = [f"Install widget {self.slug} and publish its source code."]
        if self.job is not None:
            job = self.job
            access = job.access
            inputs = json.dumps(approval_argument_view(access.inputs), ensure_ascii=True)
            if len(inputs) > 400:
                inputs = inputs[:400] + "… (See Details for the input summary.)"
            hosts = (
                ", ".join(
                    f"{host} ({binding or 'no credential'})"
                    for host, binding in sorted(access.hosts.items())
                )
                or "None"
            )
            lines.extend(
                [
                    f"Publish collector {job.slug}; refresh every {job.every_minutes} minutes.",
                    f"Private inputs: {inputs}",
                    f"Network access: {hosts}.",
                    f"Feeds: {', '.join(sorted(access.feeds))}.",
                    f"Per run: at most {access.maximum_requests} requests, "
                    f"{access.maximum_response_bytes} response bytes, "
                    f"{access.timeout_seconds} seconds; "
                    f"model budget USD {access.model_cost}.",
                    "Collection starts at the next interval boundary.",
                ]
            )
        return "\n".join(lines)
