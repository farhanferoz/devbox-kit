// Metadata only: no image conversion, Apple Events, or clipboard modification.
import AppKit
import Foundation

let board = NSPasteboard.general
let mode = CommandLine.arguments.dropFirst().first ?? "types"
let values: [String]
switch mode {
case "types":
    values = (board.types ?? []).map { $0.rawValue }
case "file-urls":
    values = (board.pasteboardItems ?? []).compactMap { item in
        guard let raw = item.string(forType: .fileURL) else { return nil }
        // Real Finder copies commonly use file:///.file/id=... references.
        // Foundation resolves these through the filesystem; URL path parsing
        // alone treats the ID as a nonexistent filename.
        return NSURL(string: raw)?.filePathURL?.absoluteString ?? raw
    }
default:
    fputs("Expected types or file-urls\n", stderr)
    exit(2)
}
do {
    FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: values))
    FileHandle.standardOutput.write(Data([10]))
} catch {
    fputs("Could not read pasteboard metadata\n", stderr)
    exit(1)
}
