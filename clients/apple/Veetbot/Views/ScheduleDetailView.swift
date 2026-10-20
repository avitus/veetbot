import SwiftUI

/// Full schedule content loaded from the authorized point-read route, read
/// as an A2 page: the instruction on a deep-paper block, then the schedule,
/// its website access, execution limits and lifecycle as record groups. The
/// summary's bounded preview is never substituted for this detail.
public struct ScheduleDetailView: View {
    @ObservedObject var model: ScheduleViewModel
    let summary: ScheduleListItemView

    public init(model: ScheduleViewModel, summary: ScheduleListItemView) {
        self.model = model
        self.summary = summary
    }

    public var body: some View {
        Group {
            if let record = model.detailRecords[summary.id] {
                detail(record)
            } else if model.isLoadingDetail(summary.id) {
                ProgressView("Loading schedule…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let message = model.detailError(for: summary.id) {
                errorState(message)
            } else {
                ProgressView("Loading schedule…")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(AppTheme.paper)
        .navigationTitle(summary.title)
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .accessibilityIdentifier("schedule.detail")
        .task {
            await model.loadDetail(summary.id)
            await model.loadWebsiteAccessProfiles()
        }
    }

    private func detail(_ record: ScheduleRecordView) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                heading(record)
                instruction(record)
                A2RecordGroup(title: "Schedule", rows: scheduleRows(record))
                A2Group(title: "Website access") { websiteAccess(record) }
                A2RecordGroup(title: "Execution", rows: executionRows(record))
                A2RecordGroup(title: "Lifecycle", rows: lifecycleRows(record))
            }
            .frame(maxWidth: 720, alignment: .leading)
            .padding(.horizontal, 24)
            .padding(.vertical, 20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func heading(_ record: ScheduleRecordView) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            #if os(macOS)
            Text(summary.title)
                .veetType(.display)
                .foregroundColor(AppTheme.ink)
                .fixedSize(horizontal: false, vertical: true)
            #endif
            HStack(spacing: 10) {
                ScheduleStateBadge(state: record.schedule.state, kind: record.schedule.stateKind)
                Text(scheduleTiming(record.revision.cadence, nextFireAt: record.schedule.nextFireAt))
                    .veetType(.ui)
                    .foregroundColor(AppTheme.inkSoft)
            }
        }
    }

    private func instruction(_ record: ScheduleRecordView) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            A2SectionHeading(title: "Instruction")
            Text(record.revision.instruction)
                .veetType(.ui)
                .foregroundColor(AppTheme.ink)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    RoundedRectangle(cornerRadius: 12, style: .continuous).fill(AppTheme.paperDeep)
                )
        }
    }

    private func scheduleRows(_ record: ScheduleRecordView) -> [A2Record] {
        var rows = [A2Record("State", scheduleDisplayText(record.schedule.state))]
        if let pauseReason = record.schedule.pauseReason {
            rows.append(A2Record("Pause reason", scheduleDisplayText(pauseReason)))
        }
        rows.append(A2Record("Cadence", scheduleCadenceSummary(record.revision.cadence)))
        if let nextFireAt = record.schedule.nextFireAt {
            rows.append(A2Record("Next firing", nextFireAt.formatted()))
        }
        rows.append(A2Record("Revision", String(record.schedule.currentRevision)))
        return rows
    }

    private func executionRows(_ record: ScheduleRecordView) -> [A2Record] {
        var rows = [
            A2Record("Agent", record.revision.agentID.uuidString),
            A2Record("Agent version", record.revision.agentVersion),
            A2Record("Policy", record.revision.policyProfile),
            A2Record(
                "Requested scopes",
                record.revision.requestedScopes.isEmpty
                    ? "None"
                    : record.revision.requestedScopes.joined(separator: ", ")
            ),
            A2Record("Run timeout", "\(record.revision.runTimeoutSeconds) seconds"),
            A2Record("Maximum steps", String(record.revision.limits.maxSteps)),
            A2Record("Maximum model calls", String(record.revision.limits.maxModelCalls)),
            A2Record("Maximum tool calls", String(record.revision.limits.maxToolCalls)),
        ]
        if let maxCost = record.revision.limits.maxCost {
            rows.append(A2Record("Maximum cost", maxCost))
        }
        rows.append(A2Record("Misfire grace", "\(record.revision.misfireGraceSeconds) seconds"))
        rows.append(A2Record("Failure limit", String(record.revision.maxConsecutiveFailures)))
        return rows
    }

    private func lifecycleRows(_ record: ScheduleRecordView) -> [A2Record] {
        [
            A2Record("Created", record.schedule.createdAt.formatted()),
            A2Record("Updated", record.schedule.updatedAt.formatted()),
            A2Record("Revision created", record.revision.createdAt.formatted()),
        ]
    }

    /// The picker reads the server's record, so a refused change snaps back to
    /// the binding the server still holds (ADR-0154).
    @ViewBuilder
    private func websiteAccess(_ record: ScheduleRecordView) -> some View {
        let scheduleID = record.schedule.id
        let bound = record.revision.browserProfileID
        VStack(alignment: .leading, spacing: 10) {
            if let profiles = model.websiteAccessProfiles {
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text("Signed-in site")
                        .veetType(.meta)
                        .foregroundColor(AppTheme.inkSoft)
                    Spacer(minLength: 0)
                    Picker(
                        "Signed-in site",
                        selection: Binding(
                            get: { bound },
                            set: { choice in
                                Task { await model.setWebsiteAccess(scheduleID, browserProfileID: choice) }
                            }
                        )
                    ) {
                        Text("None").tag(UUID?.none)
                        ForEach(
                            scheduleWebsiteAccessOptions(profiles: profiles, boundProfileID: bound)
                        ) { option in
                            Text(option.label).tag(UUID?.some(option.id))
                        }
                    }
                    .pickerStyle(.menu)
                    .labelsHidden()
                    .disabled(
                        !scheduleAllowsWebsiteAccessChange(record.schedule.stateKind)
                            || model.isSavingWebsiteAccess(scheduleID)
                    )
                    .accessibilityIdentifier("schedule.websiteAccess")
                }
            } else if let message = model.websiteAccessProfilesError {
                HStack(alignment: .firstTextBaseline) {
                    Text(message).veetType(.meta).foregroundColor(AppTheme.inkSoft)
                    Spacer()
                    Button("Retry") {
                        Task { await model.loadWebsiteAccessProfiles() }
                    }
                    .buttonStyle(SecondaryCapsuleButtonStyle())
                }
            } else {
                ProgressView("Loading sign-ins…")
            }
            if model.isSavingWebsiteAccess(scheduleID) {
                ProgressView("Saving…")
            }
            if let message = model.websiteAccessError(for: scheduleID) {
                Text(message)
                    .veetType(.meta)
                    .foregroundColor(AppTheme.red)
                    .accessibilityIdentifier("schedule.websiteAccess.error")
            }
            Text(
                "Scheduled runs can read signed-in pages on the chosen site. Posting or scrolling still asks you first."
            )
            .veetType(.meta)
            .foregroundColor(AppTheme.inkSoft)
            .fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
    }

    private func errorState(_ message: String) -> some View {
        A2EmptyState(systemImage: "exclamationmark.triangle", title: message) {
            Button("Retry") {
                Task { await model.retryDetail(summary.id) }
            }
            .buttonStyle(SecondaryCapsuleButtonStyle())
        }
    }
}
