import SwiftUI

struct CallResultSheet: View {
    let result: CallResultViewData
    let close: () -> Void
    let delete: () -> Void
    @State private var confirmingDeletion = false

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("Call result").font(.title2)
                Spacer()
                Button("Done", action: close)
            }
            if result.erased == true {
                Text("This call’s content has been deleted from Veetbot.")
                Text("Bland’s retention is managed separately.").foregroundColor(.secondary)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        if let status = result.status { Text(status.replacingOccurrences(of: "_", with: " ").capitalized) }
                        if let number = result.counterparty { Text(verbatim: number) }
                        Text("Caller identity and statements are unverified.").font(.caption).foregroundColor(.secondary)
                        if let summary = result.summary {
                            Text("Summary").font(.headline)
                            Text(verbatim: summary).textSelection(.enabled)
                        }
                        if result.summaryComplete == false { Text("The summary was truncated.").font(.caption) }
                        if let transcript = result.transcript {
                            Text("Transcript").font(.headline)
                            Text(verbatim: transcript).textSelection(.enabled)
                        }
                        if result.transcriptComplete == false { Text("The transcript was truncated.").font(.caption) }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                Button("Delete from Veetbot", role: .destructive) { confirmingDeletion = true }
                    .alert("Delete this call’s content?", isPresented: $confirmingDeletion) {
                        Button("Delete", role: .destructive, action: delete)
                        Button("Cancel", role: .cancel) {}
                    } message: {
                        Text("This removes the transcript, summary, and derived local conversation copies. Bland’s records are managed separately.")
                    }
            }
        }
        .padding(24)
        .frame(minWidth: 280, idealWidth: 540, minHeight: 300, idealHeight: 620)
    }
}
