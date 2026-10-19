import SwiftUI

/// Draws the current toast at the bottom centre of a window or sheet: an ink
/// capsule with paper text and, for a reversible change, a teal Undo after a
/// thin rule. On a touch screen it spans the width less 16 pt gutters and
/// sits above the composer or home indicator.
struct ToastHostModifier: ViewModifier {
    @ObservedObject var toasts: ToastCenter
    let bottomInset: CGFloat
    @State private var id = UUID()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content
            .overlay(alignment: .bottom) {
                ZStack {
                    if toasts.isActiveHost(id), let toast = toasts.current {
                        ToastView(toast: toast, toasts: toasts)
                            .id(toast.id)
                            .transition(reduceMotion ? .opacity : .move(edge: .bottom).combined(with: .opacity))
                    }
                }
                .animation(reduceMotion ? nil : .easeOut(duration: 0.18), value: toasts.current?.id)
                .padding(.bottom, bottomInset)
            }
            .onAppear { toasts.registerHost(id) }
            .onDisappear { toasts.unregisterHost(id) }
    }
}

extension View {
    /// Mounts a toast host; every window and sheet carries one. A screen with
    /// a composer at its foot raises the inset so the toast clears it.
    @MainActor
    func toastHost(_ toasts: ToastCenter? = nil, bottomInset: CGFloat = A2Metrics.toastBottomInset) -> some View {
        modifier(ToastHostModifier(toasts: toasts ?? .shared, bottomInset: bottomInset))
    }
}

private struct ToastView: View {
    let toast: ToastCenter.Toast
    @ObservedObject var toasts: ToastCenter

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 14) {
            Text(toast.text)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: touchLayout ? .infinity : nil, alignment: .leading)
            if let undo = toast.undo {
                #if os(macOS)
                Rectangle().fill(AppTheme.toastSoft.opacity(0.35)).frame(width: 1, height: 14)
                    .accessibilityHidden(true)
                #endif
                Button(undo.label) { Task { await toasts.performUndo() } }
                    .buttonStyle(.plain)
                    .appFont(toastFont, weight: .semibold)
                    .foregroundColor(AppTheme.toastAction)
                    .accessibilityIdentifier("toast.undo")
            }
        }
        .appFont(toastFont)
        .foregroundColor(AppTheme.toastText)
        .padding(.horizontal, touchLayout ? 18 : 16)
        .padding(.vertical, touchLayout ? 12 : 8)
        .frame(maxWidth: touchLayout ? .infinity : 520)
        .background(
            RoundedRectangle(cornerRadius: touchLayout ? 14 : 12, style: .continuous)
                .fill(AppTheme.toastBackground)
                .shadow(color: AppTheme.ink.opacity(0.18), radius: 15, y: 10)
        )
        .padding(.horizontal, 16)
        .allowsHitTesting(toast.undo != nil)
        #if os(macOS)
        .onHover { toasts.setHeld($0) }
        #endif
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isStaticText)
        .accessibilityIdentifier("toast")
        .modifier(ToastUndoAction(toast: toast, toasts: toasts))
        .onAppear { announce(toast) }
    }

    private var touchLayout: Bool {
        #if os(macOS)
        false
        #else
        true
        #endif
    }

    private var toastFont: Font.TextStyle {
        #if os(macOS)
        .callout
        #else
        .subheadline
        #endif
    }

    private func announce(_ toast: ToastCenter.Toast) {
        var announcement = AttributedString(toast.text)
        announcement.accessibilitySpeechAnnouncementPriority = .high
        AccessibilityNotification.Announcement(announcement).post()
    }
}

/// Undo is also a VoiceOver action on the toast itself.
private struct ToastUndoAction: ViewModifier {
    let toast: ToastCenter.Toast
    let toasts: ToastCenter

    func body(content: Content) -> some View {
        if let undo = toast.undo {
            content.accessibilityAction(named: Text(undo.label)) { Task { await toasts.performUndo() } }
        } else {
            content
        }
    }
}
