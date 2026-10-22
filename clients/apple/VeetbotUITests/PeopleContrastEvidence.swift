import Foundation

/// Fail-closed verification for the three iOS contrast findings in ADR-0235.
enum PeopleContrastEvidence {
    struct Measurement {
        let ratio: Double
        let foregroundPixels: Int
        let backgroundPixels: Int
    }

    private struct RGB {
        let values: [Double]

        init(_ hex: UInt32) {
            values = [Double((hex >> 16) & 255), Double((hex >> 8) & 255), Double(hex & 255)]
        }

        init(_ values: [Double]) { self.values = values }

        func near(_ other: RGB) -> Bool {
            zip(values, other.values).allSatisfy { abs($0 - $1) <= 3 }
        }

        var luminance: Double {
            let linear = values.map { value in
                let channel = value / 255
                return channel <= 0.04045 ? channel / 12.92 : pow((channel + 0.055) / 1.055, 2.4)
            }
            return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722
        }

        func isBlend(of foreground: RGB, and background: RGB) -> Bool {
            let differences = zip(foreground.values, background.values).map(-)
            let denominator = differences.reduce(0) { $0 + $1 * $1 }
            let numerator = zip(zip(values, background.values), differences).reduce(0) {
                $0 + ($1.0.0 - $1.0.1) * $1.1
            }
            let fraction = max(0, min(1, numerator / denominator))
            return (0..<3).allSatisfy {
                abs(values[$0] - (background.values[$0] + fraction * differences[$0])) <= 6
            }
        }
    }

    static func contrastRatio(foreground: UInt32, background: UInt32) -> Double {
        ratio(RGB(foreground), RGB(background))
    }

    private static func ratio(_ first: RGB, _ second: RGB) -> Double {
        (max(first.luminance, second.luminance) + 0.05) / (min(first.luminance, second.luminance) + 0.05)
    }

    static func measure(
        label: String, isContrast: Bool, isDirectory: Bool,
        width: Int, height: Int, rgba: [UInt8]
    ) -> Measurement? {
        guard isContrast, isDirectory,
              ["Everyone", "Import history", "Add person"].contains(label),
              width > 0, height > 0, width <= 4096, height <= 4096,
              width * height <= 4_000_000, rgba.count == width * height * 4 else { return nil }
        let count = width * height
        let pixels = stride(from: 0, to: rgba.count, by: 4).map { offset -> RGB in
            RGB((0..<3).map { Double(rgba[offset + $0]) })
        }
        guard stride(from: 3, to: rgba.count, by: 4).allSatisfy({ rgba[$0] == 255 }) else { return nil }
        // Pin the approved semantic ink and directory backgrounds. A palette change
        // requires fresh evidence; high contrast in an unrelated color is insufficient.
        let palettes: [(UInt32, UInt32)] = [
            (0x102923, 0xFFFFFF), (0x102923, 0xFCFAF6),
            (0xECE6D8, 0x0C211D), (0xECE6D8, 0x14302A), (0xECE6D8, 0x000000)
        ]
        for (ink, surface) in palettes {
            let foreground = RGB(ink), background = RGB(surface)
            let inkPixels = pixels.filter { $0.near(foreground) }
            let surfacePixels = pixels.filter { $0.near(background) }
            guard inkPixels.count >= max(20, count / 20), surfacePixels.count >= count / 2,
                  pixels.allSatisfy({ $0.isBlend(of: foreground, and: background) }) else { continue }
            func mean(_ samples: [RGB]) -> RGB {
                RGB((0..<3).map { channel in
                    samples.reduce(0) { $0 + $1.values[channel] } / Double(samples.count)
                })
            }
            let measured = ratio(mean(inkPixels), mean(surfacePixels))
            guard measured >= 4.5 else { continue }
            return Measurement(ratio: measured, foregroundPixels: inkPixels.count, backgroundPixels: surfacePixels.count)
        }
        return nil
    }
}
