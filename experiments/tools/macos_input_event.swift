import ApplicationServices
import Foundation

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(2)
}

guard CommandLine.arguments.count >= 3 else {
    fail("usage: macos_input_event.swift click X Y | key KEY_CODE")
}

let command = CommandLine.arguments[1]
let source = CGEventSource(stateID: .hidSystemState)

if command == "click" {
    guard CommandLine.arguments.count == 4,
          let x = Double(CommandLine.arguments[2]),
          let y = Double(CommandLine.arguments[3]) else {
        fail("click requires numeric X Y")
    }
    let point = CGPoint(x: x, y: y)
    guard let move = CGEvent(
        mouseEventSource: source,
        mouseType: .mouseMoved,
        mouseCursorPosition: point,
        mouseButton: .left
    ), let down = CGEvent(
        mouseEventSource: source,
        mouseType: .leftMouseDown,
        mouseCursorPosition: point,
        mouseButton: .left
    ), let up = CGEvent(
        mouseEventSource: source,
        mouseType: .leftMouseUp,
        mouseCursorPosition: point,
        mouseButton: .left
    ) else {
        fail("could not create mouse events")
    }
    move.post(tap: .cghidEventTap)
    usleep(40_000)
    down.post(tap: .cghidEventTap)
    usleep(60_000)
    up.post(tap: .cghidEventTap)
    print("posted_click=\(Int(x)),\(Int(y))")
} else if command == "key" {
    guard CommandLine.arguments.count == 3,
          let value = UInt16(CommandLine.arguments[2]) else {
        fail("key requires a numeric macOS virtual key code")
    }
    guard let down = CGEvent(keyboardEventSource: source, virtualKey: value, keyDown: true),
          let up = CGEvent(keyboardEventSource: source, virtualKey: value, keyDown: false) else {
        fail("could not create keyboard events")
    }
    down.post(tap: .cghidEventTap)
    usleep(60_000)
    up.post(tap: .cghidEventTap)
    print("posted_key=\(value)")
} else {
    fail("unknown command: \(command)")
}
