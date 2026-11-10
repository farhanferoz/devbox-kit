// Run on the Mac: verify PNG paste and text copy while preserving all pasteboard items.
import AppKit
import Foundation

guard CommandLine.arguments.count == 2, !CommandLine.arguments[1].hasPrefix("-") else {
    fputs("Usage: swift test_macos_clipboard.swift <ssh-host>\n", stderr)
    exit(2)
}
let board = NSPasteboard.general
let saved: [[NSPasteboard.PasteboardType: Data]] = (board.pasteboardItems ?? []).map { item in
    var result: [NSPasteboard.PasteboardType: Data] = [:]
    for type in item.types { if let data = item.data(forType: type) { result[type] = data } }
    return result
}
func restore() {
    board.clearContents()
    let items = saved.map { values -> NSPasteboardItem in
        let item = NSPasteboardItem()
        for (type, data) in values { item.setData(data, forType: type) }
        return item
    }
    board.writeObjects(items)
}
func ssh(_ command: String) throws -> Data {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
    proc.arguments = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
        CommandLine.arguments[1], command]
    let pipe = Pipe()
    proc.standardOutput = pipe
    proc.standardInput = FileHandle.nullDevice
    try proc.run()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    proc.waitUntilExit()
    if proc.terminationStatus != 0 { throw NSError(domain: "SSH", code: Int(proc.terminationStatus)) }
    return data
}
func verify() throws {
    defer { restore() }
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 2, pixelsHigh: 2,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .calibratedRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    var pixel = [255, 0, 0, 255]
    for x in 0..<2 { for y in 0..<2 { bitmap.setPixel(&pixel, atX: x, y: y) } }
    let image = bitmap.representation(using: .png, properties: [:])!
    board.clearContents()
    print("Set PNG pasteboard:", board.setData(image, forType: .png))
    let pasted = try ssh("~/.local/bin/wl-paste --type image/png")
    guard let received = NSBitmapImageRep(data: pasted), received.pixelsWide == 2, received.pixelsHigh == 2,
        let color = received.colorAt(x: 0, y: 0)?.usingColorSpace(.deviceRGB), color.redComponent > 0.9 else { throw NSError(domain: "PNG round trip mismatch", code: 1) }
    print("PASS: PNG image reaches remote with matching dimensions and pixel data")
    _ = try ssh("printf %s devbox-text-roundtrip | ~/.local/bin/wl-copy")
    guard board.string(forType: .string) == "devbox-text-roundtrip" else { throw NSError(domain: "Text copy mismatch", code: 1) }
    print("PASS: Remote text reaches Mac clipboard")
    // Finder can offer a generic icon as PNG/TIFF alongside its real file URL.
    // Deliberately use a red 2x2 icon for a blue 37x19 photo so an icon cannot pass.
    let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: folder) }
    let photo = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 37, pixelsHigh: 19,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .calibratedRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    var blue = [0, 0, 255, 255]
    for x in 0..<37 { for y in 0..<19 { photo.setPixel(&blue, atX: x, y: y) } }
    for (ext, format) in [("jpg", NSBitmapImageRep.FileType.jpeg), ("png", NSBitmapImageRep.FileType.png)] {
        let file = folder.appendingPathComponent("actual photo ' %." + ext)
        let bytes = photo.representation(using: format, properties: [:])!
        try bytes.write(to: file)
        let item = NSPasteboardItem()
        guard let reference = (file as NSURL).fileReferenceURL() else {
            throw NSError(domain: "Could not create Finder file-ID reference", code: 1)
        }
        item.setString(reference.absoluteString, forType: .fileURL)
        item.setData(image, forType: .png)
        board.clearContents()
        board.writeObjects([item])
        let offered = try ssh("~/.local/bin/xclip -selection clipboard -t TARGETS -o")
        guard String(data: offered, encoding: .utf8)?.contains("image/png") == true else {
            throw NSError(domain: "Finder file-ID reference not advertised as image", code: 1)
        }
        let actual = try ssh("~/.local/bin/wl-paste --type image/png")
        guard let decoded = NSBitmapImageRep(data: actual), decoded.pixelsWide == 37,
            decoded.pixelsHigh == 19,
            let color = decoded.colorAt(x: 10, y: 10)?.usingColorSpace(.deviceRGB),
            color.blueComponent > 0.9, color.redComponent < 0.1 else {
            throw NSError(domain: "File paste returned wrong image/icon", code: 1)
        }
        if ext == "png" && actual != bytes { throw NSError(domain: "File PNG bytes differ", code: 1) }
        print("PASS: Finder file-ID " + ext + " transfers actual 37x19 blue photo, not 2x2 red file icon")
    }

}
do { try verify(); print("Mac pasteboard restored") }
catch { fputs("Clipboard verification failed: \(error)\n", stderr); exit(1) }
