import AVFoundation
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(2)
}

guard CommandLine.arguments.count == 7 else {
    fail(
        "usage: mp4_to_png_sequence.swift INPUT.mp4 OUTPUT_DIRECTORY "
            + "START_SECONDS DURATION_SECONDS FPS WIDTH"
    )
}

let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let outputDirectory = URL(
    fileURLWithPath: CommandLine.arguments[2],
    isDirectory: true
)
guard let startSeconds = Double(CommandLine.arguments[3]), startSeconds >= 0 else {
    fail("START_SECONDS must be non-negative")
}
guard let durationSeconds = Double(CommandLine.arguments[4]), durationSeconds > 0 else {
    fail("DURATION_SECONDS must be positive")
}
guard let fps = Int(CommandLine.arguments[5]), fps > 0 else {
    fail("FPS must be a positive integer")
}
guard let width = Int(CommandLine.arguments[6]), width > 0 else {
    fail("WIDTH must be a positive integer")
}

let fileManager = FileManager.default
guard fileManager.fileExists(atPath: inputURL.path) else {
    fail("input does not exist: \(inputURL.path)")
}
do {
    try fileManager.createDirectory(
        at: outputDirectory,
        withIntermediateDirectories: true
    )
} catch {
    fail("cannot create output directory: \(error)")
}

let asset = AVURLAsset(url: inputURL)
let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.maximumSize = CGSize(width: width, height: width)
generator.requestedTimeToleranceBefore = .zero
generator.requestedTimeToleranceAfter = .zero

let frameCount = max(1, Int((durationSeconds * Double(fps)).rounded(.down)))
for frameIndex in 0..<frameCount {
    let seconds = startSeconds + (Double(frameIndex) / Double(fps))
    let time = CMTime(seconds: seconds, preferredTimescale: 600)
    let image: CGImage
    do {
        image = try generator.copyCGImage(at: time, actualTime: nil)
    } catch {
        fail("cannot decode frame \(frameIndex) at \(seconds)s: \(error)")
    }
    let frameURL = outputDirectory.appendingPathComponent(
        String(format: "frame-%04d.png", frameIndex)
    )
    guard let destination = CGImageDestinationCreateWithURL(
        frameURL as CFURL,
        UTType.png.identifier as CFString,
        1,
        nil
    ) else {
        fail("cannot create frame destination: \(frameURL.path)")
    }
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination) else {
        fail("cannot write frame: \(frameURL.path)")
    }
}

print("decoded_frames=\(frameCount) fps=\(fps) max_width=\(width)")
