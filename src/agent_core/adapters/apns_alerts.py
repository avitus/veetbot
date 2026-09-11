"""Lock-screen presentation derived only from the closed notification payload."""

from agent_core.domain.notifications import NotificationKind, NotificationPayload
from agent_core.domain.runs import RunStatus
from agent_core.domain.schedules import OccurrenceDisposition

_SCHEDULE_OUTCOMES = {
    RunStatus.COMPLETED: (
        "Scheduled run completed",
        "The scheduled task finished successfully. Open Veetbot to view the result.",
    ),
    RunStatus.FAILED: (
        "Scheduled run failed",
        "The scheduled task stopped before finishing. Open Veetbot to see the error.",
    ),
    RunStatus.CANCELLED: (
        "Scheduled run cancelled",
        "The scheduled task was cancelled before it finished.",
    ),
    OccurrenceDisposition.MISSED: (
        "Scheduled run missed",
        "The start window passed before this task could run.",
    ),
    OccurrenceDisposition.SKIPPED_OVERLAP: (
        "Scheduled run skipped",
        "An earlier run of this schedule was still active.",
    ),
    OccurrenceDisposition.AUTHORIZATION_FAILED: (
        "Scheduled run blocked",
        "The required access was no longer available. Review the schedule in Veetbot.",
    ),
    OccurrenceDisposition.CONFIGURATION_FAILED: (
        "Scheduled run needs attention",
        "The schedule could not start with its current configuration. Review it in Veetbot.",
    ),
}

_HEALTH_SIGNALS = {
    "readiness_local": "Application readiness",
    "readiness_public": "Public API availability",
    "tls_days_left": "TLS certificate renewal",
    "units_active": "Service availability",
    "units_failed": "Service health",
    "worker_watchdog": "Worker stability",
    "queue_lag": "Run queue delay",
    "schedule_lag": "Schedule delay",
    "notify_backlog": "Notification backlog",
    "disk_free": "Disk space",
    "postgres_ready": "Database availability",
    "backup_fresh": "Backup freshness",
    "rehearsal_verdict": "Backup restore verification",
    "secrets_unescrowed": "Secret backup coverage",
    "reboot_required": "Server reboot",
    "release_rollback": "Release rollback",
}
_SEVERITIES = {"info": "Info", "warn": "Warning", "critical": "Critical"}


def apns_alert(payload: NotificationPayload) -> dict[str, str]:
    """Keep wire/deep-link titles stable while making the visible alert useful."""

    title = payload.title
    match payload.kind:
        case NotificationKind.APPROVAL_REQUESTED:
            body = (
                f"Review {payload.tool_name} to let this run continue."
                if payload.tool_name
                else "This run is paused. Open Veetbot to review the requested action."
            )
        case NotificationKind.QUESTION_ASKED:
            body = "This run is waiting for your answer. Open Veetbot to respond."
        case NotificationKind.RUN_FAILED:
            body = "This run stopped before finishing. Open Veetbot to see the error."
        case NotificationKind.SCHEDULE_RUN_FINISHED | NotificationKind.SCHEDULE_OCCURRENCE_SKIPPED:
            # NotificationPayload validates the status for each kind.
            assert isinstance(payload.status, (RunStatus, OccurrenceDisposition))
            title, body = _SCHEDULE_OUTCOMES[payload.status]
        case NotificationKind.OPS_ALERT | NotificationKind.OPS_RECOVERED:
            signal = _HEALTH_SIGNALS.get(payload.signal or "", "A production health check")
            if payload.kind is NotificationKind.OPS_RECOVERED:
                body = f"{signal} has recovered."
            elif payload.signal == "release_rollback":
                body = "The application was rolled back to a previous release."
            else:
                severity = _SEVERITIES.get(payload.severity or "", "Alert")
                body = f"{severity}: {signal} needs attention."
        case NotificationKind.TEST:
            body = "Notifications are working on this device."
        case NotificationKind.DEVICE_INVOCATION:
            if payload.tool_name == "device.sms.send":
                title = "Text message ready to review"
                body = (
                    "Open Veetbot on your iPhone to review the recipient and message, "
                    "then choose whether to send."
                )
            elif payload.tool_name:
                body = f"Open Veetbot on the requested device to review {payload.tool_name}."
            else:
                body = "Open Veetbot on the requested device to review and complete the action."
    return {"title": title, "body": body}
