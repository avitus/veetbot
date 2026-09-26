import Foundation
import Testing

@testable import VeetbotCore

/// ADR-0129, 0129-design §6.2, §6.3 and §14 item 3: the `browser.act`
/// approval card names the action, the element and the page, quotes website
/// text as such, and offers Allow for this task only with an offer.
@Suite struct ApprovalCardViewStructureTests {
    private func approval(
        _ name: String, edit: (inout [String: Any]) -> Void = { _ in }
    ) throws -> ApprovalView {
        var object = try WireModelsTests.contractExample("approval_views", name, key: "approval")
        edit(&object)
        return try JSONDecoder.server.decode(
            ApprovalView.self, from: JSONSerialization.data(withJSONObject: object)
        )
    }

    private func arguments(_ object: inout [String: Any], _ edit: (inout [String: Any]) -> Void) {
        var arguments = object["arguments"] as? [String: Any] ?? [:]
        edit(&arguments)
        object["arguments"] = arguments
    }

    private func source(_ path: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: packageRoot.appendingPathComponent(path), encoding: .utf8)
    }

    @Test
    func eachKindRendersWithItsOwnVerb() throws {
        var verbs: [String] = []
        for kind in ["click", "type", "select", "check", "press", "scroll"] {
            let view = try approval("offered") { object in
                arguments(&object) { $0["kind"] = kind; $0["key"] = "Enter"; $0["scroll_delta_y"] = 400 }
            }
            let card = try #require(BrowserActionApprovalPresentation(approval: view))
            verbs.append(card.verb)
        }
        #expect(verbs == ["Click", "Type", "Choose", "Toggle", "Press", "Scroll"])
        let press = try #require(
            BrowserActionApprovalPresentation(
                approval: try approval("offered") { object in
                    arguments(&object) { $0["kind"] = "press"; $0["key"] = "Enter" }
                }
            )
        )
        #expect(press.detail == "Enter")
        let unknown = try #require(
            BrowserActionApprovalPresentation(
                approval: try approval("offered") { object in arguments(&object) { $0["kind"] = "hover" } }
            )
        )
        #expect(unknown.verb == "Act on")
    }

    @Test
    func theCardNamesTheElementAndThePage() throws {
        let card = try #require(BrowserActionApprovalPresentation(approval: try approval("offered")))
        #expect(card.elementRole == "button")
        #expect(card.elementName == "el gato")
        #expect(card.pageLocation == "www.duolingo.com/lesson/unit-3")
        #expect(card.pageTitle == "Duolingo")
        #expect(card.consequenceBadge == nil)
        #expect(card.refusedNotice == nil)
        #expect(card.notCoveredText == nil)
    }

    @Test
    func shownOnScreenAppearsOnlyWithElementText() throws {
        let plain = try #require(BrowserActionApprovalPresentation(approval: try approval("offered")))
        #expect(plain.shownOnScreen == nil)
        let disguised = try #require(BrowserActionApprovalPresentation(approval: try approval("not_covered")))
        #expect(disguised.shownOnScreen == "Pay $12.99")
        #expect(disguised.dialogName == "Try Super free")
        #expect(disguised.consequenceBadge == "Payment")
    }

    @Test
    func allowForThisTaskIsOfferedOnlyWithAnOfferOnAPendingApproval() throws {
        #expect(try #require(BrowserActionApprovalPresentation(approval: try approval("offered"))).offersTaskGrant)
        #expect(!(try #require(BrowserActionApprovalPresentation(approval: try approval("not_covered"))).offersTaskGrant))
        #expect(
            !(try #require(BrowserActionApprovalPresentation(approval: try approval("resolved_for_task"))).offersTaskGrant)
        )
    }

    @Test
    func typedTextIsShownExceptInASensitiveField() throws {
        let hidden = try #require(BrowserActionApprovalPresentation(approval: try approval("typed_text_redacted")))
        #expect(hidden.typedText == .hidden)
        #expect(hidden.refusedNotice == "This will be refused")
        #expect(hidden.consequenceBadge == "Sign-in change")
        let answer = try #require(
            BrowserActionApprovalPresentation(
                approval: try approval("offered") { object in
                    arguments(&object) {
                        $0["kind"] = "type"; $0["field"] = "text"; $0["text"] = "la manzana"
                    }
                }
            )
        )
        #expect(answer.typedText == .visible("la manzana"))
        #expect(
            try #require(BrowserActionApprovalPresentation(approval: try approval("offered"))).typedText == nil
        )
    }

    @Test
    func anUndescribedOrOtherApprovalFallsBackToTheExistingCard() throws {
        #expect(BrowserActionApprovalPresentation(approval: try approval("undescribed")) == nil)
        #expect(
            BrowserActionApprovalPresentation(
                approval: try approval("offered") { $0["tool_name"] = "sandbox.run_command" }
            ) == nil
        )
    }

    /// ADR-0129 decision 8: the option comes from the website, so the card
    /// quotes and labels it instead of setting it beside Veetbot's verb.
    @Test
    func aChosenOptionIsWebsiteTextNotPartOfTheVerb() throws {
        let option = "a safe lesson answer (verified)"
        let card = try #require(
            BrowserActionApprovalPresentation(
                approval: try approval("offered") { object in
                    arguments(&object) { $0["kind"] = "select"; $0["option"] = option }
                }
            )
        )
        #expect(card.verb == "Choose")
        #expect(card.detail == nil)
        #expect(card.option == option)
        let click = try #require(BrowserActionApprovalPresentation(approval: try approval("offered")))
        #expect(click.option == nil)
        let cardSource = try source("Veetbot/Views/BrowserActionApprovalCard.swift")
        #expect(cardSource.contains("labelledQuote(\"Option from the website\", option)"))
    }

    @Test
    func noReferenceRevisionOrQueryIsEverRendered() throws {
        let view = try approval("offered") { object in
            arguments(&object) {
                $0["ref"] = "e-ref-sentinel"
                $0["expected_revision"] = 41
                $0["page_path"] = "/lesson/unit-3"
            }
        }
        let card = try #require(BrowserActionApprovalPresentation(approval: view))
        let rendered = [
            card.verb, card.elementRole, card.elementName, card.shownOnScreen, card.pageLocation,
            card.pageTitle, card.dialogName, card.option, card.detail, card.consequenceBadge,
            card.refusedNotice, card.notCoveredText,
        ].compactMap { $0 }.joined(separator: " ")
        #expect(!rendered.contains("e-ref-sentinel"))
        #expect(!rendered.contains("41"))
        let cardSource = try source("Veetbot/Views/BrowserActionApprovalCard.swift")
        #expect(!cardSource.contains("\"ref\""))
        #expect(!cardSource.contains("expected_revision"))
        #expect(!cardSource.contains(".arguments"))
    }

    @Test
    func websiteTextUsesVerbatimRenderingCappedAtThreeLines() throws {
        let cardSource = try source("Veetbot/Views/BrowserActionApprovalCard.swift")
        #expect(cardSource.contains("Text(verbatim: \"“\\(text)”\")"))
        #expect(cardSource.contains(".lineLimit(expanded ? nil : 3)"))
        #expect(cardSource.contains("Text from the website"))
        #expect(cardSource.contains("Shows on screen as"))
        #expect(cardSource.contains("In a dialog titled"))
        #expect(cardSource.contains("Hidden: sensitive field"))
        #expect(cardSource.contains("Text(verbatim: offer.summary)"))
        #expect(!cardSource.contains("AttributedString(markdown"))
        #expect(!cardSource.contains("MarkdownContentView"))
        for label in ["\"Allow once\"", "\"Allow for this task\"", "\"Deny\"", "\"Allow\"", "\"Cancel\""] {
            #expect(cardSource.contains(label))
        }
        let activity = try source("Veetbot/Views/ActivityViews.swift")
        #expect(activity.contains("BrowserActionApprovalPresentation(approval: approval"))
        #expect(activity.contains("BrowserActionApprovalCard("))
    }

    @Test
    func everyClosedNotCoveredReasonHasItsOwnPlainWords() throws {
        let reasons = try #require(try WireModelsTests.contract()["not_covered_reasons"] as? [String])
        var sentences: Set<String> = []
        for reason in reasons {
            let text = BrowserActionApprovalPresentation.notCoveredText(for: reason, pathPrefix: "/lesson")
            #expect(text != BrowserActionApprovalPresentation.genericNotCoveredText, "\(reason)")
            #expect(!text.contains("browser.task_grant"), "\(reason)")
            sentences.insert(text)
        }
        #expect(sentences.count == reasons.count)
        #expect(
            BrowserActionApprovalPresentation.notCoveredText(for: "browser.task_grant.excluded.payment", pathPrefix: nil)
                == "Your task permission doesn't cover this: it looks like a payment."
        )
        #expect(
            BrowserActionApprovalPresentation.notCoveredText(for: "browser.task_grant.outside_prefix", pathPrefix: "/lesson")
                == "Your task permission doesn't cover this: the page is outside /lesson."
        )
        #expect(
            BrowserActionApprovalPresentation.notCoveredText(for: "browser.task_grant.new_rule", pathPrefix: nil)
                == BrowserActionApprovalPresentation.genericNotCoveredText
        )
        let card = try #require(BrowserActionApprovalPresentation(approval: try approval("not_covered")))
        #expect(card.notCoveredText == "Your task permission doesn't cover this: it looks like a payment.")
    }
}
