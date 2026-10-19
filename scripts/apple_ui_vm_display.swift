// Switch the Tart guest's main display to a WIDTH x HEIGHT point mode at 2x.
//
// Tart offers the configured size as a mode, but the guest boots into the
// image's saved 1024x768 mode, where the Mac UI cases' windows do not fit.
// The guest forgets the choice at shutdown, so the VM runner applies it on
// every boot.
import CoreGraphics
import Foundation

let arguments = CommandLine.arguments
guard arguments.count == 3, let width = Int(arguments[1]), let height = Int(arguments[2]) else {
    FileHandle.standardError.write(Data("usage: apple_ui_vm_display.swift WIDTH HEIGHT\n".utf8))
    exit(2)
}
let display = CGMainDisplayID()
let options = [kCGDisplayShowDuplicateLowResolutionModes: kCFBooleanTrue] as CFDictionary
let modes = (CGDisplayCopyAllDisplayModes(display, options) as? [CGDisplayMode]) ?? []
guard let mode = modes.first(where: {
    $0.width == width && $0.height == height && $0.pixelWidth == 2 * width
}) else {
    FileHandle.standardError.write(Data("The guest offers no \(width)x\(height) point mode at 2x.\n".utf8))
    exit(1)
}
var configuration: CGDisplayConfigRef?
guard CGBeginDisplayConfiguration(&configuration) == .success,
      CGConfigureDisplayWithDisplayMode(configuration, display, mode, nil) == .success,
      CGCompleteDisplayConfiguration(configuration, .forSession) == .success
else {
    FileHandle.standardError.write(Data("The guest refused the \(width)x\(height) display mode.\n".utf8))
    exit(1)
}
