import Foundation
import Testing

@testable import VeetbotCore

@Suite struct AppearancePreferencesTests {
    @Test
    func testDefaultsUseSystemSizingAndRoundedTypography() throws {
        let defaults = try isolatedDefaults()
        let preferences = AppearancePreferences(defaults: defaults)

        #expect(preferences.textSize == .system)
        #expect(preferences.fontStyle == .rounded)
    }

    @Test
    func testAppearanceChoicesPersistAcrossInstances() throws {
        let defaults = try isolatedDefaults()
        let preferences = AppearancePreferences(defaults: defaults)
        preferences.textSize = .large
        preferences.fontStyle = .serif

        let reloaded = AppearancePreferences(defaults: defaults)

        #expect(reloaded.textSize == .large)
        #expect(reloaded.fontStyle == .serif)
    }

    private func isolatedDefaults() throws -> UserDefaults {
        let suiteName = "AppearancePreferencesTests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        return defaults
    }
}
