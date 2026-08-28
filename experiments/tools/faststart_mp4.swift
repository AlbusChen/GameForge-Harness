import AVFoundation
import Foundation

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(2)
}

guard CommandLine.arguments.count == 3 else {
    fail("usage: faststart_mp4.swift INPUT.mp4 OUTPUT.mp4")
}

let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let outputURL = URL(fileURLWithPath: CommandLine.arguments[2])
let asset = AVURLAsset(url: inputURL)

guard let exporter = AVAssetExportSession(
    asset: asset,
    presetName: AVAssetExportPresetPassthrough
) else {
    fail("cannot create passthrough exporter")
}
guard exporter.supportedFileTypes.contains(.mp4) else {
    fail("asset cannot be exported as MP4 without transcoding")
}

exporter.outputURL = outputURL
exporter.outputFileType = .mp4
exporter.shouldOptimizeForNetworkUse = true

let completion = DispatchSemaphore(value: 0)
exporter.exportAsynchronously { completion.signal() }
completion.wait()

guard exporter.status == .completed else {
    fail("export failed: \(exporter.error?.localizedDescription ?? "unknown error")")
}

print("faststart_mp4 input=\(inputURL.path) output=\(outputURL.path)")
