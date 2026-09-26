import Foundation

/// What a `browser.act` approval card says (ADR-0129, 0129-design §6 and
/// §14 item 3). Website-authored strings stay separate from Veetbot's own
/// words so the card can quote and label them; the element reference, the
/// revision, the page query and link targets are never read.
public struct BrowserActionApprovalPresentation: Equatable, Sendable {
    public enum TypedText: Equatable, Sendable {
        case visible(String)
        case hidden
    }

    /// Veetbot's own word for the action kind.
    public let verb: String
    /// Website text: the element's role attribute and accessible name.
    public let elementRole: String?
    public let elementName: String?
    /// Website text: the visible text, only when it differs from the name.
    public let shownOnScreen: String?
    /// The page's host and path, as the server validated them.
    public let pageLocation: String?
    /// Website text: the page title and the dialog around the element.
    public let pageTitle: String?
    public let dialogName: String?
    /// Website text: the option a `select` chooses (ADR-0129 decision 8).
    public let option: String?
    public let typedText: TypedText?
    /// Veetbot's words for the key, scroll direction or new state an action
    /// carries. Never website text: a chosen option is `option`.
    public let detail: String?
    public let consequenceBadge: String?
    public let refusedNotice: String?
    public let notCoveredText: String?
    public let offersTaskGrant: Bool

    /// Nil unless the approval is a described `browser.act` view; an
    /// undescribed one keeps the existing card.
    public init?(approval: ApprovalView, activeGrant: BrowserTaskGrantView? = nil) {
        guard let view = approval.browserAction, view.described else { return nil }
        verb = Self.verbs[view.kind] ?? "Act on"
        elementRole = view.elementRole
        elementName = view.elementName
        shownOnScreen = view.elementText
        pageTitle = view.pageTitle
        dialogName = view.elementContext
        option = view.kind == "select" ? view.option : nil
        if let origin = view.pageOrigin, let host = URL(string: origin)?.host {
            pageLocation = host + (view.pagePath ?? "")
        } else {
            pageLocation = nil
        }
        if view.kind == "type" {
            let sensitive = ["password", "one_time_code", "payment"].contains(view.field ?? "")
            if sensitive || view.text == nil || view.text == "[REDACTED]" {
                typedText = .hidden
            } else {
                typedText = .visible(view.text ?? "")
            }
        } else {
            typedText = nil
        }
        switch view.kind {
        case "press": detail = view.key
        case "scroll": detail = view.scrollDeltaY.map { $0 < 0 ? "Up" : "Down" }
        case "check": detail = view.checked.map { $0 ? "Now on" : "Now off" }
        default: detail = nil
        }
        consequenceBadge = view.consequence.flatMap { Self.consequences[$0] }
        refusedNotice = view.refused ? "This will be refused" : nil
        let prefix = activeGrant.flatMap { grant in
            grant.id == approval.taskGrantNotCovered?.grantID ? grant.pathPrefix : nil
        }
        notCoveredText = approval.taskGrantNotCovered.map {
            Self.notCoveredText(for: $0.reason, pathPrefix: prefix)
        }
        offersTaskGrant = approval.status.isPending && approval.taskGrantOffer != nil
    }

    public static let genericNotCoveredText = "Your task permission doesn't cover this action."

    /// Plain words for a closed not-covered reason (0129-design §15); any
    /// other value gets the generic sentence.
    public static func notCoveredText(for reason: String, pathPrefix: String?) -> String {
        let outside = pathPrefix.map { "outside \($0)" } ?? "outside the pages you allowed"
        let phrases: [String: String] = [
            "expired": "it has expired.",
            "exhausted": "it has used all its actions.",
            "revoked": "it was stopped.",
            "ended": "it has ended.",
            "unavailable": "task permissions are unavailable right now.",
            "policy_changed": "Veetbot's safety rules changed since you allowed it.",
            "profile_changed": "the website was signed in to again since you allowed it.",
            "agent_changed": "Veetbot was updated since you allowed it.",
            "scope_removed": "this site is no longer allowed for task permissions.",
            "run_not_eligible": "it only covers chats you are running yourself.",
            "turn_not_browser_only": "Veetbot used another tool since your last message.",
            "outside_origin": "the page is on another website.",
            "outside_prefix": "the page is \(outside).",
            "sensitive_path": "the page looks like an account, settings or payment page.",
            "unnamed_element": "the control has no name Veetbot can read.",
            "field_not_covered": "it doesn't cover this kind of field.",
            "text_too_long": "the text is too long.",
            "text_not_covered": "the text looks like an address, a link, a number or a password.",
            "text_budget_exhausted": "it has already typed as much as it may.",
            "link_outside_prefix": "the link leads \(outside).",
            "form_outside_prefix": "the form sends \(outside).",
            "facts_unavailable": "Veetbot couldn't read enough about this control.",
            "excluded.payment": "it looks like a payment.",
            "excluded.purchase": "it looks like a purchase.",
            "excluded.account_recovery": "it looks like account recovery.",
            "excluded.authentication_change": "it looks like a sign-in or password change.",
            "excluded.permission_change": "it looks like a permission change.",
            "excluded.legal_acceptance": "it looks like accepting terms.",
            "excluded.publication": "it looks like posting or sending a message.",
            "excluded.destructive": "it looks like deleting something.",
            "excluded.file_transfer": "it looks like uploading or downloading a file.",
            "excluded.security_change": "it looks like a security setting.",
        ]
        let prefix = "browser.task_grant."
        guard reason.hasPrefix(prefix), let phrase = phrases[String(reason.dropFirst(prefix.count))]
        else { return genericNotCoveredText }
        return "Your task permission doesn't cover this: \(phrase)"
    }

    private static let verbs = [
        "click": "Click", "type": "Type", "select": "Choose", "check": "Toggle",
        "press": "Press", "scroll": "Scroll",
    ]

    /// Named consequences only; `routine` and `unknown` carry no badge.
    private static let consequences = [
        "payment": "Payment",
        "purchase": "Purchase",
        "account_recovery": "Account recovery",
        "authentication_change": "Sign-in change",
        "permission_change": "Permission change",
        "legal_acceptance": "Legal agreement",
        "publication": "Posts or sends",
        "destructive": "Deletes",
        "file_transfer": "File transfer",
        "security_change": "Security change",
    ]
}
