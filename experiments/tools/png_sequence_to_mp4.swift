import AVFoundation
import CoreGraphics
import Foundation
import ImageIO

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(2)
}

guard CommandLine.arguments.count == 4 else {
    fail("usage: png_sequence_to_mp4.swift FRAMES_DIRECTORY OUTPUT.mp4 FPS")
}

let framesDirectory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let outputURL = URL(fileURLWithPath: CommandLine.arguments[2])
guard let fps = Int32(CommandLine.arguments[3]), fps > 0 else {
    fail("FPS must be a positive integer")
}

let fileManager = FileManager.default
let frameURLs: [URL]
do {
    frameURLs = try fileManager.contentsOfDirectory(
        at: framesDirectory,
        includingPropertiesForKeys: nil,
        options: [.skipsHiddenFiles]
    ).filter { $0.pathExtension.lowercased() == "png" }
     .sorted { $0.lastPathComponent < $1.lastPathComponent }
} catch {
    fail("cannot list frames: \(error)")
}
guard let firstURL = frameURLs.first,
      let firstSource = CGImageSourceCreateWithURL(firstURL as CFURL, nil),
      let firstImage = CGImageSourceCreateImageAtIndex(firstSource, 0, nil) else {
    fail("no readable PNG frames found")
}

let width = firstImage.width
let height = firstImage.height
try? fileManager.removeItem(at: outputURL)

let writer: AVAssetWriter
do {
    writer = try AVAssetWriter(outputURL: outputURL, fileType: .mp4)
} catch {
    fail("cannot create asset writer: \(error)")
}
writer.shouldOptimizeForNetworkUse = true

let settings: [String: Any] = [
    AVVideoCodecKey: AVVideoCodecType.h264,
    AVVideoWidthKey: width,
    AVVideoHeightKey: height,
    AVVideoCompressionPropertiesKey: [
        AVVideoAverageBitRateKey: max(1_500_000, width * height * 4),
        AVVideoExpectedSourceFrameRateKey: fps,
        AVVideoMaxKeyFrameIntervalKey: fps * 2,
    ],
]
let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
input.expectsMediaDataInRealTime = false
let pixelAttributes: [String: Any] = [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
    kCVPixelBufferWidthKey as String: width,
    kCVPixelBufferHeightKey as String: height,
    kCVPixelBufferCGImageCompatibilityKey as String: true,
    kCVPixelBufferCGBitmapContextCompatibilityKey as String: true,
]
let adaptor = AVAssetWriterInputPixelBufferAdaptor(
    assetWriterInput: input,
    sourcePixelBufferAttributes: pixelAttributes
)
guard writer.canAdd(input) else { fail("writer cannot add video input") }
writer.add(input)
guard writer.startWriting() else {
    fail("writer failed to start: \(writer.error?.localizedDescription ?? "unknown error")")
}
writer.startSession(atSourceTime: .zero)

for (index, frameURL) in frameURLs.enumerated() {
    while !input.isReadyForMoreMediaData {
        Thread.sleep(forTimeInterval: 0.002)
    }
    guard let source = CGImageSourceCreateWithURL(frameURL as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        writer.cancelWriting()
        fail("cannot decode frame \(frameURL.path)")
    }
    guard image.width == width, image.height == height else {
        writer.cancelWriting()
        fail("frame dimensions changed at \(frameURL.lastPathComponent)")
    }
    var optionalBuffer: CVPixelBuffer?
    guard let pool = adaptor.pixelBufferPool,
          CVPixelBufferPoolCreatePixelBuffer(nil, pool, &optionalBuffer) == kCVReturnSuccess,
          let buffer = optionalBuffer else {
        writer.cancelWriting()
        fail("cannot allocate pixel buffer")
    }
    CVPixelBufferLockBaseAddress(buffer, [])
    guard let baseAddress = CVPixelBufferGetBaseAddress(buffer),
          let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
          let context = CGContext(
            data: baseAddress,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: CVPixelBufferGetBytesPerRow(buffer),
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue
                | CGBitmapInfo.byteOrder32Little.rawValue
          ) else {
        CVPixelBufferUnlockBaseAddress(buffer, [])
        writer.cancelWriting()
        fail("cannot create bitmap context")
    }
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    CVPixelBufferUnlockBaseAddress(buffer, [])
    let presentationTime = CMTime(value: Int64(index), timescale: fps)
    guard adaptor.append(buffer, withPresentationTime: presentationTime) else {
        writer.cancelWriting()
        fail("cannot append frame \(index): \(writer.error?.localizedDescription ?? "unknown error")")
    }
}

input.markAsFinished()
let completion = DispatchSemaphore(value: 0)
writer.finishWriting { completion.signal() }
completion.wait()
guard writer.status == .completed else {
    fail("writer failed: \(writer.error?.localizedDescription ?? "unknown error")")
}
print("encoded_frames=\(frameURLs.count) width=\(width) height=\(height) fps=\(fps)")
