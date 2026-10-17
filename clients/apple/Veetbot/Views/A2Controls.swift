import SwiftUI

/// The shared controls of the A2 Dock design: capsule buttons in three
/// weights, the teal mark beside an answer, the amber line that says what the
/// transcript is waiting for, and the sidebar's section headers.
enum A2Metrics {
    #if os(macOS)
    static let controlHeight: CGFloat = 28
    static let rowHeight: CGFloat = 28
    static let composerRadius: CGFloat = 22
    #else
    static let controlHeight: CGFloat = 36
    static let rowHeight: CGFloat = 44
    static let composerRadius: CGFloat = 24
    #endif
    static let cardRadius: CGFloat = 14
    static let transcriptMaxWidth: CGFloat = 760
}

/// A filled teal capsule: the one action the owner most likely takes.
struct PrimaryCapsuleButtonStyle: ButtonStyle {
    var expands = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .appFont(.callout, weight: .medium)
            .lineLimit(1)
            .padding(.horizontal, 14)
            .frame(maxWidth: expands ? .infinity : nil, minHeight: A2Metrics.controlHeight)
            .foregroundColor(AppTheme.onTeal)
            .background(Capsule().fill(AppTheme.tealFill))
            .opacity(configuration.isPressed ? 0.85 : 1)
            .contentShape(Capsule())
    }
}

/// A hairline capsule: an alternative that still deserves a border.
struct SecondaryCapsuleButtonStyle: ButtonStyle {
    var expands = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .appFont(.callout, weight: .medium)
            .lineLimit(1)
            .padding(.horizontal, 14)
            .frame(maxWidth: expands ? .infinity : nil, minHeight: A2Metrics.controlHeight)
            .foregroundColor(AppTheme.ink)
            .background(
                Capsule().fill(configuration.isPressed ? AppTheme.field : Color.clear)
            )
            .overlay(Capsule().stroke(AppTheme.hairlineStrong))
            .contentShape(Capsule())
    }
}

/// Text only, in soft ink: the quiet way out.
struct QuietCapsuleButtonStyle: ButtonStyle {
    var expands = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .appFont(.callout, weight: .medium)
            .lineLimit(1)
            .padding(.horizontal, 12)
            .frame(maxWidth: expands ? .infinity : nil, minHeight: A2Metrics.controlHeight)
            .foregroundColor(configuration.isPressed ? AppTheme.ink : AppTheme.inkSoft)
            .background(
                Capsule().fill(configuration.isPressed ? AppTheme.field : Color.clear)
            )
            .contentShape(Capsule())
    }
}

/// A square icon button in soft ink, for the composer and the sidebar.
struct QuietIconButtonStyle: ButtonStyle {
    var side: CGFloat = A2Metrics.controlHeight

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .frame(width: side, height: side)
            .foregroundColor(configuration.isPressed ? AppTheme.ink : AppTheme.inkSoft)
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .fill(configuration.isPressed ? AppTheme.field : Color.clear)
            )
            .contentShape(RoundedRectangle(cornerRadius: 8))
    }
}

/// The small teal mark that opens an assistant answer.
struct AssistantMark: View {
    var body: some View {
        Circle()
            .fill(AppTheme.teal)
            .frame(width: 8, height: 8)
            .background(Circle().fill(AppTheme.tealWash).padding(-3))
            .accessibilityHidden(true)
    }
}

/// One amber line in the transcript: the run is waiting on the owner, and the
/// card to answer it sits above the composer.
struct WaitingLine: View {
    let text: String

    var body: some View {
        Label {
            Text(text)
        } icon: {
            Image(systemName: "flag")
        }
        .appFont(.callout, weight: .semibold)
        .foregroundColor(AppTheme.amberText)
        .accessibilityIdentifier("chat.waiting")
    }
}

/// A sidebar section heading in the A2 voice: small, semibold, soft ink.
struct SidebarSectionLabel: View {
    let title: String

    var body: some View {
        Text(title)
            .appFont(.caption, weight: .semibold)
            .foregroundColor(AppTheme.inkSoft)
            .textCase(nil)
    }
}

/// The amber or teal dot beside a Needs you row.
struct AttentionDot: View {
    let kind: SidebarAttention.Kind

    var body: some View {
        Circle()
            .fill(kind == .approval ? AppTheme.amber : AppTheme.teal)
            .frame(width: 7, height: 7)
            .accessibilityHidden(true)
    }
}
