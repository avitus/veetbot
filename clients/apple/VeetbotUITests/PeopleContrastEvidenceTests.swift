import Testing

@Suite struct PeopleContrastEvidenceTests {
    private func pixels(foreground: UInt32, background: UInt32) -> [UInt8] {
        (0..<400).flatMap { index -> [UInt8] in
            let color = index < 100 ? foreground : background
            return [UInt8((color >> 16) & 255), UInt8((color >> 8) & 255), UInt8(color & 255), 255]
        }
    }

    private func measure(
        _ rgba: [UInt8], label: String = "Everyone", isContrast: Bool = true,
        isDirectory: Bool = true, width: Int = 20, height: Int = 20
    ) -> PeopleContrastEvidence.Measurement? {
        PeopleContrastEvidence.measure(
            label: label, isContrast: isContrast, isDirectory: isDirectory,
            width: width, height: height, rgba: rgba
        )
    }

    @Test(arguments: ["Everyone", "Import history", "Add person"])
    func acceptsRenderedLightAndDarkInk(label: String) throws {
        for (foreground, background): (UInt32, UInt32) in [
            (0x102923, 0xFFFFFF), (0x102923, 0xFCFAF6),
            (0xECE6D8, 0x0C211D), (0xECE6D8, 0x14302A), (0xECE6D8, 0x000000)
        ] {
            let result = try #require(measure(pixels(foreground: foreground, background: background), label: label))
            #expect(result.ratio >= 4.5)
            #expect(result.foregroundPixels == 100)
            #expect(result.backgroundPixels == 300)
        }
    }

    @Test func measuresWCAGLuminanceAndAcceptsAntialiasing() throws {
        #expect(abs(PeopleContrastEvidence.contrastRatio(foreground: 0x000000, background: 0xFFFFFF) - 21) < 0.001)
        #expect(PeopleContrastEvidence.contrastRatio(foreground: 0xFFFFFF, background: 0xFFFFFF) == 1)
        #expect(PeopleContrastEvidence.contrastRatio(foreground: 0x777777, background: 0xFFFFFF) < 4.5)
        var antialiased = pixels(foreground: 0x102923, background: 0xFFFFFF)
        antialiased.replaceSubrange(0..<4, with: [136, 148, 145, 255])
        let result = try #require(measure(antialiased))
        #expect(result.foregroundPixels == 99)
        #expect(result.ratio >= 4.5)
    }

    @Test func rejectsOtherFindingsAndMissingEvidence() {
        let valid = pixels(foreground: 0x102923, background: 0xFFFFFF)
        #expect(measure(valid, label: "Another control") == nil)
        #expect(measure(valid, isContrast: false) == nil)
        #expect(measure(valid, isDirectory: false) == nil)
        #expect(measure([]) == nil)
        #expect(measure(valid, width: 0) == nil)
        #expect(measure(valid, width: Int.max) == nil)
        #expect(measure(Array(valid.dropLast())) == nil)
    }

    @Test func rejectsLowContrastWrongColorsAndInsufficientInk() {
        #expect(measure(pixels(foreground: 0xAAAAAA, background: 0xFFFFFF)) == nil)
        #expect(measure(pixels(foreground: 0x102923, background: 0x2F4640)) == nil)
        #expect(measure(pixels(foreground: 0xFF0000, background: 0xFFFFFF)) == nil)
        #expect(measure(pixels(foreground: 0xFFFFFF, background: 0xFFFFFF)) == nil)
        var sparse = pixels(foreground: 0xFFFFFF, background: 0xFFFFFF)
        sparse.replaceSubrange(0..<4, with: [0x10, 0x29, 0x23, 255])
        #expect(measure(sparse) == nil)
        var transparent = pixels(foreground: 0x102923, background: 0xFFFFFF)
        transparent[3] = 0
        #expect(measure(transparent) == nil)
        var unrelated = pixels(foreground: 0x102923, background: 0xFFFFFF)
        unrelated.replaceSubrange(0..<4, with: [255, 0, 0, 255])
        #expect(measure(unrelated) == nil)
    }
}
