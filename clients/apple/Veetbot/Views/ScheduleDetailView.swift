import SwiftUI

/// Full schedule content loaded from the authorized point-read route. The
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
        .navigationTitle(summary.title)
        .accessibilityIdentifier("schedule.detail")
        .task {
            await model.loadDetail(summary.id)
            await model.loadWebsiteAccessProfiles()
        }
    }

    private func detail(_ record: ScheduleRecordView) -> some View {
        List {
            Section("Instruction") {
                Text(record.revision.instruction)
            }

            Section("Schedule") {
                HStack(alignment: .firstTextBaseline) {
                    Text("State").appFont(.caption).foregroundColor(.secondary)
                    Spacer()
                    ScheduleStateBadge(
                        state: record.schedule.state,
                        kind: record.schedule.stateKind
                    )
                }
                if let pauseReason = record.schedule.pauseReason {
                    ScheduleKeyValueRow(
                        key: "Pause reason",
                        value: scheduleDisplayText(pauseReason)
                    )
                }
                ScheduleKeyValueRow(
                    key: "Cadence",
                    value: scheduleCadenceSummary(record.revision.cadence)
                )
                if let nextFireAt = record.schedule.nextFireAt {
                    ScheduleKeyValueRow(key: "Next firing", value: nextFireAt.formatted())
                }
                ScheduleKeyValueRow(
                    key: "Revision",
                    value: String(record.schedule.currentRevision)
                )
            }

            Section {
                websiteAccess(record)
            } header: {
                Text("Website access")
            } footer: {
                Text(
                    "Scheduled runs can read signed-in pages on the chosen site. Posting or scrolling still asks you first."
                )
            }

            Section("Execution") {
                ScheduleKeyValueRow(key: "Agent", value: record.revision.agentID.uuidString)
                ScheduleKeyValueRow(key: "Agent version", value: record.revision.agentVersion)
                ScheduleKeyValueRow(key: "Policy", value: record.revision.policyProfile)
                ScheduleKeyValueRow(
                    key: "Requested scopes",
                    value: record.revision.requestedScopes.isEmpty
                        ? "None"
                        : record.revision.requestedScopes.joined(separator: ", ")
                )
                ScheduleKeyValueRow(
                    key: "Run timeout",
                    value: "\(record.revision.runTimeoutSeconds) seconds"
                )
                ScheduleKeyValueRow(
                    key: "Maximum steps",
                    value: String(record.revision.limits.maxSteps)
                )
                ScheduleKeyValueRow(
                    key: "Maximum model calls",
                    value: String(record.revision.limits.maxModelCalls)
                )
                ScheduleKeyValueRow(
                    key: "Maximum tool calls",
                    value: String(record.revision.limits.maxToolCalls)
                )
                if let maxCost = record.revision.limits.maxCost {
                    ScheduleKeyValueRow(key: "Maximum cost", value: maxCost)
                }
                ScheduleKeyValueRow(
                    key: "Misfire grace",
                    value: "\(record.revision.misfireGraceSeconds) seconds"
                )
                ScheduleKeyValueRow(
                    key: "Failure limit",
                    value: String(record.revision.maxConsecutiveFailures)
                )
            }

            Section("Lifecycle") {
                ScheduleKeyValueRow(key: "Created", value: record.schedule.createdAt.formatted())
                ScheduleKeyValueRow(key: "Updated", value: record.schedule.updatedAt.formatted())
                ScheduleKeyValueRow(
                    key: "Revision created",
                    value: record.revision.createdAt.formatted()
                )
            }
        }
    }

    /// The picker reads the server's record, so a refused change snaps back to
    /// the binding the server still holds (ADR-0154).
    @ViewBuilder
    private func websiteAccess(_ record: ScheduleRecordView) -> some View {
        let scheduleID = record.schedule.id
        let bound = record.revision.browserProfileID
        if let profiles = model.websiteAccessProfiles {
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
            .disabled(
                !scheduleAllowsWebsiteAccessChange(record.schedule.stateKind)
                    || model.isSavingWebsiteAccess(scheduleID)
            )
            .accessibilityIdentifier("schedule.websiteAccess")
        } else if let message = model.websiteAccessProfilesError {
            HStack(alignment: .firstTextBaseline) {
                Text(message).appFont(.caption).foregroundColor(.secondary)
                Spacer()
                Button("Retry") {
                    Task { await model.loadWebsiteAccessProfiles() }
                }
            }
        } else {
            ProgressView("Loading sign-ins…")
        }
        if model.isSavingWebsiteAccess(scheduleID) {
            ProgressView("Saving…")
        }
        if let message = model.websiteAccessError(for: scheduleID) {
            Text(message)
                .appFont(.caption)
                .foregroundColor(.red)
                .accessibilityIdentifier("schedule.websiteAccess.error")
        }
    }

    private func errorState(_ message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle")
                .font(.largeTitle)
                .foregroundColor(.secondary)
            Text(message)
                .multilineTextAlignment(.center)
                .foregroundColor(.secondary)
            Button("Retry") {
                Task { await model.retryDetail(summary.id) }
            }
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct ScheduleKeyValueRow: View {
    let key: String
    let value: String

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(key).appFont(.caption).foregroundColor(.secondary)
            Spacer()
            Text(value)
                .appFont(.caption)
                .multilineTextAlignment(.trailing)
        }
    }
}
