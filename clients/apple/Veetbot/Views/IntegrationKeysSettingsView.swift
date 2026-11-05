import SwiftUI

#if os(iOS)
import UIKit
#else
import AppKit
#endif

struct IntegrationKeysSettingsSection: View {
    let api: VeetbotAPIClient?
    @State private var keys: [IntegrationKey] = []
    @State private var error: String?
    @State private var unavailable = false
    @State private var loaded = false
    @State private var busy = false
    @State private var editing: IntegrationKeyDraft?
    @State private var deleting: IntegrationKey?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Save keys here, then share only their references in Chat. Use narrowly scoped, read-only keys. Saving does not test the provider or grant a job access.")
                .veetType(.meta)
            if unavailable {
                Text("Integration keys aren't enabled on this Veetbot server.")
            } else {
                ForEach(keys) { key in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(key.label).veetType(.ui)
                        Text("\(key.origin) · \(key.authScheme) · Revision \(key.revision)")
                        Text("Created \(key.createdAt.formatted()) · Updated \(key.updatedAt.formatted())")
                            .veetType(.meta)
                        HStack {
                            Button("Copy reference") { copyReference(key.reference) }
                            Button("Replace") { editing = IntegrationKeyDraft(key: key) }
                            Button("Delete", role: .destructive) { deleting = key }
                        }
                    }
                    Divider()
                }
                if loaded {
                    if keys.isEmpty { Text("No integration keys yet.") }
                    Button("Add key") { editing = IntegrationKeyDraft() }
                        .disabled(keys.count >= 100)
                }
                if let error { Text(error).foregroundStyle(AppTheme.red) }
                Button("Refresh") { Task { await load() } }
            }
        }
        .disabled(busy)
        .task { await load() }
        .sheet(item: $editing) { draft in
            IntegrationKeyForm(api: api, draft: draft) { Task { await load() } }
        }
        .confirmationDialog("Delete this integration key?", isPresented: Binding(
            get: { deleting != nil }, set: { if !$0 { deleting = nil } }
        )) {
            if let key = deleting {
                Button("Delete key", role: .destructive) { Task { await remove(key) } }
            }
        } message: {
            Text("Approved jobs will lose access. If the key was compromised, revoke it at the provider too. Old backups may still contain it.")
        }
    }

    private func load() async {
        guard let api, !busy else { return }
        busy = true
        defer { busy = false }
        do {
            keys = try await api.listIntegrationKeys().secrets
            loaded = true
            unavailable = false
            error = nil
        } catch HTTPTransportError.api(let failure) where failure.statusCode == 404 || failure.statusCode == 405 {
            keys = []
            unavailable = true
        } catch {
            self.error = "Couldn't load integration keys. Check your connection and Settings access, then refresh."
        }
    }

    private func remove(_ key: IntegrationKey) async {
        guard let api, !busy else { return }
        busy = true
        do {
            try await api.deleteIntegrationKey(key)
            keys.removeAll { $0.id == key.id }
            error = nil
        } catch {
            self.error = "Couldn't delete the key. It may have changed; refresh before trying again."
        }
        busy = false
        deleting = nil
    }

    private func copyReference(_ reference: String) {
        #if os(iOS)
        UIPasteboard.general.string = reference
        #else
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(reference, forType: .string)
        #endif
    }
}

/// Contains no key value; stable identity makes a lost-response retry safe.
struct IntegrationKeyDraft: Identifiable {
    let id: UUID
    let key: IntegrationKey?
    init(key: IntegrationKey? = nil) {
        self.key = key
        id = key?.id ?? UUID()
    }
}

private struct IntegrationKeyForm: View {
    let api: VeetbotAPIClient?
    let draft: IntegrationKeyDraft
    let saved: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var label = ""
    @State private var origin = ""
    @State private var scheme = "bearer"
    @State private var value = ""
    @State private var busy = false
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(draft.key == nil ? "Add integration key" : "Replace integration key").veetType(.ui)
            if let key = draft.key {
                Text("\(key.label) · \(key.origin) · \(key.authScheme)")
                Text("Existing approved jobs will use the replacement on their next request.")
            } else {
                TextField("Label", text: $label, prompt: .a2Prompt("Label (not the key)"))
                TextField("HTTPS origin", text: $origin, prompt: .a2Prompt("https://api.example.com"))
                Picker("Authentication", selection: $scheme) {
                    Text("Bearer token").tag("bearer")
                    Text("X-API-Key").tag("x-api-key")
                }
            }
            SecureField("Key", text: $value, prompt: .a2Prompt("Key"))
                .autocorrectionDisabled()
                .privacySensitive()
            Text("The saved value cannot be viewed or copied. Use a read-only key; GET alone does not guarantee a harmless request.")
                .veetType(.meta)
            if let error { Text(error).foregroundStyle(AppTheme.red) }
            HStack {
                Button("Cancel") { value = ""; dismiss() }
                Button("Save") { Task { await save() } }
                    .disabled(value.isEmpty || (draft.key == nil && (label.isEmpty || origin.isEmpty)))
            }
        }
        .padding(24)
        .frame(minWidth: 300, idealWidth: 480)
        .disabled(busy)
        .interactiveDismissDisabled(busy)
        .onDisappear { value = "" }
    }

    private func save() async {
        guard let api, !busy else { return }
        busy = true
        defer { busy = false }
        do {
            _ = try await api.saveIntegrationKey(
                id: draft.id, label: draft.key?.label ?? label,
                origin: draft.key?.origin ?? origin, authScheme: draft.key?.authScheme ?? scheme,
                value: value, expectedRevision: draft.key?.revision ?? 0
            )
            value = ""
            dismiss()
            saved()
        } catch HTTPTransportError.api(let failure) where failure.statusCode == 409 {
            value = ""
            error = "The key changed or the key limit was reached. Close this form and refresh."
        } catch {
            // Never display a server or transport description of secret input.
            self.error = "Couldn't save the key. Check the origin, key format, connection and Settings access. Retry, or cancel and refresh."
        }
    }
}
