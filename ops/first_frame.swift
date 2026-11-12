// Writes the exact first frame of a video as a JPEG. Usage: first_frame <in.mp4> <out.jpg> <quality 0-1>
import AVFoundation
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

let args = CommandLine.arguments
let asset = AVURLAsset(url: URL(fileURLWithPath: args[1]))
let generator = AVAssetImageGenerator(asset: asset)
generator.appliesPreferredTrackTransform = true
generator.requestedTimeToleranceBefore = .zero  // frame 0 itself, not the nearest keyframe
generator.requestedTimeToleranceAfter = .zero
let image = try generator.copyCGImage(at: .zero, actualTime: nil)
let out = URL(fileURLWithPath: args[2])
guard let destination = CGImageDestinationCreateWithURL(out as CFURL, UTType.jpeg.identifier as CFString, 1, nil) else { exit(1) }
CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: Double(args[3])!] as CFDictionary)
guard CGImageDestinationFinalize(destination) else { exit(1) }
print("\(image.width)x\(image.height)")
