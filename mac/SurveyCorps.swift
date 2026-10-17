// Native window around app.py. Handles Outlook drags itself (file promises), which browsers don't reliably do.
import Cocoa
import WebKit

// app.py is found relative to this source file, so rebuild if you move the project.
let appPy = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
    .appendingPathComponent("app.py").path
let base = URL(string: "http://127.0.0.1:8765")!
let petSize: CGFloat = 96  // pet icon size in points

class DropWebView: WKWebView {
    static let promiseTypes = NSFilePromiseReceiver.readableDraggedTypes.map { NSPasteboard.PasteboardType($0) }

    override init(frame: NSRect, configuration: WKWebViewConfiguration) {
        super.init(frame: frame, configuration: configuration)
        registerForDraggedTypes(registeredDraggedTypes + Self.promiseTypes + [.fileURL])
    }
    required init?(coder: NSCoder) { fatalError() }

    func isFile(_ s: NSDraggingInfo) -> Bool {
        let types = s.draggingPasteboard.types ?? []
        return types.contains(.fileURL) || types.contains(where: Self.promiseTypes.contains)
    }
    // super still runs so the page shows its drop highlight
    override func draggingEntered(_ s: NSDraggingInfo) -> NSDragOperation {
        let op = super.draggingEntered(s); return isFile(s) ? .copy : op
    }
    override func draggingUpdated(_ s: NSDraggingInfo) -> NSDragOperation {
        let op = super.draggingUpdated(s); return isFile(s) ? .copy : op
    }
    override func performDragOperation(_ s: NSDraggingInfo) -> Bool {
        guard isFile(s) else { return super.performDragOperation(s) }
        let pb = s.draggingPasteboard
        if let urls = pb.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL], !urls.isEmpty {
            urls.forEach(upload)
        } else if let promises = pb.readObjects(forClasses: [NSFilePromiseReceiver.self]) as? [NSFilePromiseReceiver] {
            let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            for p in promises {
                p.receivePromisedFiles(atDestination: dir, options: [:], operationQueue: .main) { url, err in
                    if err == nil { self.upload(url) }
                }
            }
        }
        return true
    }

    func upload(_ file: URL) {
        guard let data = try? Data(contentsOf: file) else { return }
        var c = URLComponents(url: base.appendingPathComponent("api/drop"), resolvingAgainstBaseURL: false)!
        c.queryItems = [URLQueryItem(name: "name", value: file.lastPathComponent)]
        var req = URLRequest(url: c.url!)
        req.httpMethod = "POST"
        URLSession.shared.uploadTask(with: req, from: data) { _, _, _ in
            DispatchQueue.main.async { self.evaluateJavaScript("drop.classList.remove('over');load()") }
        }.resume()
    }
}

// Original plush-chibi pet: shaggy blond hair, big blue eyes, serious face, holding a letter. 64x64 box.
func drawPet(_ b: NSRect) {
    let k = min(b.width, b.height) / 64
    NSGraphicsContext.current?.cgContext.scaleBy(x: k, y: k)
    func rgb(_ r: CGFloat, _ g: CGFloat, _ bl: CGFloat, _ a: CGFloat = 1) -> NSColor {
        NSColor(srgbRed: r / 255, green: g / 255, blue: bl / 255, alpha: a)
    }
    func oval(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) -> NSBezierPath {
        NSBezierPath(ovalIn: NSRect(x: x, y: y, width: w, height: h))
    }
    let hair = rgb(250, 226, 130), hairDark = rgb(214, 178, 72), skin = rgb(248, 216, 192), skinEdge = rgb(222, 180, 152)
    let ink = rgb(35, 28, 26)
    let sh = NSShadow(); sh.shadowBlurRadius = 3; sh.shadowOffset = NSSize(width: 0, height: -1)
    sh.shadowColor = NSColor.black.withAlphaComponent(0.35)

    // back of the hair
    NSGraphicsContext.saveGraphicsState(); sh.set()
    let back = NSBezierPath(roundedRect: NSRect(x: 7, y: 20, width: 50, height: 42), xRadius: 23, yRadius: 22)
    hair.setFill(); back.fill()
    NSGraphicsContext.restoreGraphicsState()

    // wide plush face
    let face = oval(11, 15, 42, 36); skin.setFill(); face.fill(); skinEdge.setStroke(); face.lineWidth = 0.8; face.stroke()

    // eyes: large, round, thick upper lash, glossy blue iris
    for (cx, dir): (CGFloat, CGFloat) in [(23.5, -1), (40.5, 1)] {
        let eye = NSBezierPath(roundedRect: NSRect(x: cx - 5, y: 25.5, width: 10, height: 10), xRadius: 4.8, yRadius: 4.8)
        NSColor.white.setFill(); eye.fill()
        NSGraphicsContext.saveGraphicsState(); eye.addClip()
        let iris = oval(cx - 4.3, 25.5, 8.6, 9.6)
        NSGradient(starting: rgb(15, 70, 160), ending: rgb(80, 185, 245))!.draw(in: iris, angle: -90)
        rgb(10, 45, 110).setStroke(); iris.lineWidth = 0.7; iris.stroke()
        rgb(15, 40, 95, 0.8).setFill(); oval(cx - 2, 28.5, 4, 4.4).fill()
        NSColor.white.setFill(); oval(cx - dir * 1.4 - 1.5, 30.6, 3, 3).fill()      // main highlight
        NSColor.white.withAlphaComponent(0.9).setFill(); oval(cx + dir * 1.6 - 0.7, 27.0, 1.4, 1.4).fill()
        NSGraphicsContext.restoreGraphicsState()
        let lash = NSBezierPath(); lash.lineWidth = 1.9; lash.lineCapStyle = .round; lash.lineJoinStyle = .round
        lash.move(to: NSPoint(x: cx - dir * 4.6, y: 33.3))
        lash.curve(to: NSPoint(x: cx + dir * 5.4, y: 33.6), controlPoint1: NSPoint(x: cx - dir * 2.5, y: 36.2),
                   controlPoint2: NSPoint(x: cx + dir * 3, y: 36.4))
        lash.line(to: NSPoint(x: cx + dir * 6.2, y: 32.2))
        ink.setStroke(); lash.stroke()
        let lower = NSBezierPath(); lower.move(to: NSPoint(x: cx - 2.5, y: 25.2)); lower.line(to: NSPoint(x: cx + 2.5, y: 25.2))
        rgb(120, 80, 70, 0.5).setStroke(); lower.lineWidth = 0.6; lower.stroke()
    }

    // mouth: small, flat, serious
    let mouth = NSBezierPath(); mouth.move(to: NSPoint(x: 29.8, y: 21.6))
    mouth.curve(to: NSPoint(x: 34.2, y: 21.6), controlPoint1: NSPoint(x: 31, y: 21.7), controlPoint2: NSPoint(x: 33, y: 21.7))
    ink.setStroke(); mouth.lineWidth = 0.9; mouth.lineCapStyle = .round; mouth.stroke()

    // bangs: shaggy pointed strands over the forehead, long side locks
    let bangs = NSBezierPath(); bangs.move(to: NSPoint(x: 8, y: 40))
    bangs.curve(to: NSPoint(x: 32, y: 62), controlPoint1: NSPoint(x: 7, y: 56), controlPoint2: NSPoint(x: 18, y: 62))
    bangs.curve(to: NSPoint(x: 56, y: 40), controlPoint1: NSPoint(x: 46, y: 62), controlPoint2: NSPoint(x: 57, y: 56))
    for (x, y): (CGFloat, CGFloat) in [(53, 22), (50.5, 39), (48.5, 33), (46, 42), (43, 36.5), (40, 43.5), (37, 35.5),
                                       (34, 44), (31.5, 37.5), (28.5, 43.5), (25.5, 36.5), (22, 42.5), (18.5, 33), (15.5, 39), (11, 22)] {
        bangs.line(to: NSPoint(x: x, y: y))
    }
    bangs.close(); bangs.lineJoinStyle = .round
    hair.setFill(); bangs.fill(); hairDark.setStroke(); bangs.lineWidth = 0.9; bangs.stroke()
    for (x0, y0, x1, y1): (CGFloat, CGFloat, CGFloat, CGFloat) in [(20, 55, 18.5, 40), (28, 58, 25.5, 41), (38, 58, 37, 42),
                                                                   (46, 55, 48.5, 40), (13, 48, 12.5, 32), (51, 48, 52, 32)] {
        let s = NSBezierPath(); s.move(to: NSPoint(x: x0, y: y0)); s.line(to: NSPoint(x: x1, y: y1))
        hairDark.withAlphaComponent(0.55).setStroke(); s.lineWidth = 0.7; s.lineCapStyle = .round; s.stroke()
    }
    let shine = NSBezierPath(); shine.move(to: NSPoint(x: 20, y: 57.5))
    shine.curve(to: NSPoint(x: 30, y: 60.3), controlPoint1: NSPoint(x: 23, y: 59.5), controlPoint2: NSPoint(x: 26.5, y: 60.3))
    NSColor.white.withAlphaComponent(0.65).setStroke(); shine.lineWidth = 1.5; shine.lineCapStyle = .round; shine.stroke()

    // thick slanted brows showing between the strands
    for (a, c): ((CGFloat, CGFloat), (CGFloat, CGFloat)) in [((19.5, 38.4), (27, 37.4)), ((44.5, 38.4), (37, 37.4))] {
        let br = NSBezierPath(); br.move(to: NSPoint(x: a.0, y: a.1)); br.line(to: NSPoint(x: c.0, y: c.1))
        ink.setStroke(); br.lineWidth = 1.3; br.lineCapStyle = .round; br.stroke()
    }

    // the letter, held with two little hands
    NSGraphicsContext.saveGraphicsState(); sh.set()
    let env = NSBezierPath(roundedRect: NSRect(x: 15, y: 1, width: 34, height: 16), xRadius: 3, yRadius: 3)
    rgb(255, 250, 240).setFill(); env.fill()
    NSGraphicsContext.restoreGraphicsState()
    rgb(40, 96, 190).setStroke(); env.lineWidth = 1.4; env.stroke()
    let flap = NSBezierPath(); flap.move(to: NSPoint(x: 16, y: 15.5)); flap.line(to: NSPoint(x: 32, y: 7.5)); flap.line(to: NSPoint(x: 48, y: 15.5))
    flap.lineWidth = 1.5; flap.lineJoinStyle = .round; flap.stroke()
    for x: CGFloat in [11.5, 45.5] {
        let hand = oval(x, 5, 7, 7); skin.setFill(); hand.fill(); skinEdge.setStroke(); hand.lineWidth = 0.9; hand.stroke()
    }
}

// Floating pet that stays on every desktop. Click: show/hide Survey Corps. Drag: move it.
class PetView: NSView {
    var onClick: () -> Void = {}
    var grab = NSPoint.zero, moved = false
    override func draw(_ r: NSRect) { drawPet(bounds) }
    override func acceptsFirstMouse(for e: NSEvent?) -> Bool { true }
    override func mouseDown(with e: NSEvent) { grab = e.locationInWindow; moved = false }
    override func mouseDragged(with e: NSEvent) {
        moved = true
        let p = NSEvent.mouseLocation
        window?.setFrameOrigin(NSPoint(x: p.x - grab.x, y: p.y - grab.y))
    }
    override func mouseUp(with e: NSEvent) {
        if moved { window?.saveFrame(usingName: "pet") } else { onClick() }
    }
}

class App: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var pet: NSPanel!
    let server = Process()

    func applicationDidFinishLaunching(_ n: Notification) {
        let logPath = NSHomeDirectory() + "/Library/Logs/SurveyCorps.log"
        FileManager.default.createFile(atPath: logPath, contents: nil)
        let log = FileHandle(forWritingAtPath: logPath)
        server.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        server.arguments = [appPy, "--no-browser"]
        server.standardOutput = log
        server.standardError = log
        try? server.run()  // if a server is already running, this one exits and we use that one

        let web = DropWebView(frame: .zero, configuration: WKWebViewConfiguration())
        web.navigationDelegate = self
        web.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Survey Corps"
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("main")
        window.isReleasedWhenClosed = false
        window.makeKeyAndOrderFront(nil)
        web.load(URLRequest(url: base))

        pet = NSPanel(contentRect: NSRect(x: 0, y: 0, width: petSize, height: petSize),
                      styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        pet.level = .floating
        pet.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
        pet.backgroundColor = .clear
        pet.isOpaque = false
        pet.hasShadow = false
        let view = PetView()
        view.toolTip = "Survey Corps: click to show/hide, drag to move"
        view.onClick = { [unowned self] in self.toggle() }
        pet.contentView = view
        if !pet.setFrameUsingName("pet"), let f = NSScreen.main?.visibleFrame {
            pet.setFrameOrigin(NSPoint(x: f.maxX - petSize - 26, y: f.minY + 20))  // bottom-right corner
        }
        pet.setContentSize(NSSize(width: petSize, height: petSize))  // saved frame may hold an old size
        pet.orderFrontRegardless()
    }

    func toggle() {
        if window.isVisible && NSApp.isActive {
            window.orderOut(nil)
        } else {
            NSApp.activate(ignoringOtherApps: true)
            window.makeKeyAndOrderFront(nil)
        }
    }

    // server takes a moment to start; keep retrying
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { w.load(URLRequest(url: base)) }
    }

    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil); return true
    }

    func applicationWillTerminate(_ n: Notification) {
        if server.isRunning { server.terminate() }
    }

    // Links in email bodies (target=_blank) open in the default browser, not inside the app.
    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for a: WKNavigationAction,
                 windowFeatures f: WKWindowFeatures) -> WKWebView? {
        if let url = a.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // WKWebView has no built-in confirm()/prompt(); the page uses both.
    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo,
                 completionHandler done: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = m
        a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
        done(a.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ w: WKWebView, runJavaScriptTextInputPanelWithPrompt p: String, defaultText d: String?,
                 initiatedByFrame f: WKFrameInfo, completionHandler done: @escaping (String?) -> Void) {
        let a = NSAlert(); a.messageText = p
        a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
        let tf = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        tf.stringValue = d ?? ""
        a.accessoryView = tf
        a.window.initialFirstResponder = tf
        done(a.runModal() == .alertFirstButtonReturn ? tf.stringValue : nil)
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate

// Menus: Cmd+Q/W, plus Edit so Cmd+C/V/Z work in text fields.
let mainMenu = NSMenu()
let appMenu = NSMenu()
appMenu.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
appMenu.addItem(withTitle: "Quit Survey Corps", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
let editMenu = NSMenu(title: "Edit")
for (title, sel, key) in [("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), ("Cut", "cut:", "x"),
                          ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")] {
    editMenu.addItem(withTitle: title, action: Selector(sel), keyEquivalent: key)
}
for m in [appMenu, editMenu] {
    let item = NSMenuItem(); item.submenu = m; mainMenu.addItem(item)
}
app.mainMenu = mainMenu
app.setActivationPolicy(.regular)
app.run()
