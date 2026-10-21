import SwiftUI

/// A call's result is a reading surface; the unverified transcript is disclosed on demand.
struct CallResultSheet: View {
    let result: CallResultViewData
    let close: () -> Void
    let delete: () -> Void
    var followUp: ((CallFollowUpAction) -> Void)? = nil
    @State private var confirmingDeletion = false
    @State private var transcriptExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Label("Call · \(result.counterparty ?? "Unknown caller")", systemImage: "phone")
                        .veetType(.meta, weight: .semibold).foregroundColor(AppTheme.inkSoft)
                    HStack(spacing: 6) {
                        if let duration = result.durationText { Text(duration).accessibilityIdentifier("call.duration") }
                        if let date = result.createdAt { Text(date, format: .dateTime.month(.abbreviated).day().hour().minute()) }
                    }.veetType(.meta).foregroundColor(AppTheme.inkFaint)
                }
                Spacer()
                Button(action: close) { Image(systemName: "xmark") }
                    .buttonStyle(QuietCapsuleButtonStyle()).accessibilityLabel("Close")
            }.padding(.horizontal, 24).padding(.top, 20).padding(.bottom, 12)
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if result.erased == true {
                        Text("This call’s content has been deleted from Veetbot.").veetType(.display)
                        Text("Bland’s retention is managed separately.").veetType(.meta).foregroundColor(AppTheme.inkSoft)
                    } else {
                        if let summary = result.summary, !summary.isEmpty {
                            Text(verbatim: summary).veetType(.display).textSelection(.enabled)
                        } else {
                            Text("Call result").veetType(.display)
                        }
                        if let status = result.status {
                            Label(status.replacingOccurrences(of: "_", with: " ").capitalized, systemImage: "phone")
                                .veetType(.ui).foregroundColor(AppTheme.inkSoft)
                        }
                        Text("Caller identity and statements are unverified.")
                            .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                        if result.summaryComplete == false { Text("The summary was truncated.").veetType(.meta) }
                        if let transcript = result.transcript {
                            Button { transcriptExpanded.toggle() } label: {
                                Label("What was said", systemImage: transcriptExpanded ? "chevron.down" : "chevron.right")
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain).veetType(.meta).foregroundColor(AppTheme.inkSoft)
                            .accessibilityIdentifier("call.transcript")
                            .accessibilityValue(transcriptExpanded ? "Expanded" : "Collapsed")
                            if transcriptExpanded {
                                Text(verbatim: transcript).veetType(.ui).textSelection(.enabled)
                                    .frame(maxWidth: .infinity, alignment: .leading).padding(.top, 12)
                                if result.transcriptComplete == false { Text("The transcript was truncated.").veetType(.meta) }
                            }
                        }
                        Text("Veetbot keeps this summary and transcript for up to 30 days. Bland’s records are managed separately.")
                            .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                    }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 24).padding(.bottom, 20)
            }
            HairlineDivider()
            ViewThatFits(in: .horizontal) {
                HStack {
                    secondaryActions
                    Spacer()
                    primaryActions
                }
                VStack(spacing: 12) {
                    HStack { secondaryActions; Spacer() }
                    HStack { Spacer(); primaryActions }
                }
            }.padding(16)
        }
        .background(AppTheme.surface)
        .tint(AppTheme.tealFill)
        .sheetFrame(minWidth: 280, macMinWidth: 480, idealWidth: 560, minHeight: 300, idealHeight: 480)
    }

    @ViewBuilder private var secondaryActions: some View {
        if result.canFollowUp, let followUp {
            Button { followUp(.somethingWrong) } label: { Image(systemName: "flag") }
                .buttonStyle(QuietCapsuleButtonStyle()).accessibilityLabel("Something is wrong")
                .help("Review this call in Chat").accessibilityIdentifier("call.something-wrong")
        }
        if result.erased != true {
            Button("Delete from Veetbot", role: .destructive) { confirmingDeletion = true }
                .buttonStyle(QuietCapsuleButtonStyle())
                .alert("Delete this call’s content?", isPresented: $confirmingDeletion) {
                    Button("Delete", role: .destructive, action: delete)
                    Button("Cancel", role: .cancel) {}
                } message: {
                    Text("This removes the transcript, summary, and derived local conversation copies. Bland’s records are managed separately.")
                }
        }
    }

    @ViewBuilder private var primaryActions: some View {
        if result.canFollowUp, let followUp {
            Button { followUp(.callAgain) } label: { Label("Call again", systemImage: "phone") }
                .buttonStyle(SecondaryCapsuleButtonStyle())
                .help("Prepare a new call request in Chat").accessibilityIdentifier("call.again")
        }
        Button("Done", action: close).primaryCapsule()
    }

}
