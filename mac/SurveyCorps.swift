// Native window around app.py. Handles Outlook drags itself (file promises), which browsers don't reliably do.
import Cocoa
import WebKit

// app.py is found relative to this source file, so rebuild if you move the project.
let appPy = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
    .appendingPathComponent("app.py").path
let base = URL(string: "http://127.0.0.1:8765")!
let feedURL = base.appendingPathComponent("feed")
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

// Original plush-chibi pet: swept-back blond hair, thick brows, square jaw, stern look, holding a letter. 64x64 box.
// scout: the second pet (Scout window) — Hange-style researcher: messy brown ponytail, glasses, excited grin, magnifying glass.
func drawPet(_ b: NSRect, scout: Bool = false) {
    let k = min(b.width, b.height) / 64
    NSGraphicsContext.current?.cgContext.scaleBy(x: k, y: k)
    func rgb(_ r: CGFloat, _ g: CGFloat, _ bl: CGFloat, _ a: CGFloat = 1) -> NSColor {
        NSColor(srgbRed: r / 255, green: g / 255, blue: bl / 255, alpha: a)
    }
    func oval(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) -> NSBezierPath {
        NSBezierPath(ovalIn: NSRect(x: x, y: y, width: w, height: h))
    }
    let hair = scout ? rgb(128, 84, 52) : rgb(250, 226, 130), hairDark = scout ? rgb(82, 50, 28) : rgb(214, 178, 72), skin = rgb(248, 216, 192), skinEdge = rgb(222, 180, 152)
    let ink = rgb(35, 28, 26)
    let sh = NSShadow(); sh.shadowBlurRadius = 3; sh.shadowOffset = NSSize(width: 0, height: -1)
    sh.shadowColor = NSColor.black.withAlphaComponent(0.35)

    // back of the hair (scout: plus a messy high ponytail poking up on the right)
    NSGraphicsContext.saveGraphicsState(); sh.set()
    if scout {
        let tail = NSBezierPath(); tail.move(to: NSPoint(x: 38, y: 56))
        for (x, y): (CGFloat, CGFloat) in [(43, 63.5), (46.5, 59.5), (52, 63.5), (52, 58.5), (58, 60), (54.5, 54), (60, 52), (50, 50)] {
            tail.line(to: NSPoint(x: x, y: y))
        }
        tail.close(); tail.lineJoinStyle = .round
        hair.setFill(); tail.fill(); hairDark.setStroke(); tail.lineWidth = 0.9; tail.stroke()
    }
    let back = NSBezierPath(roundedRect: NSRect(x: 7, y: 20, width: 50, height: 42), xRadius: 23, yRadius: 22)
    hair.setFill(); back.fill()
    if scout {  // hair tie at the ponytail's base
        rgb(176, 48, 48).setFill(); NSBezierPath(roundedRect: NSRect(x: 42.5, y: 56.5, width: 7, height: 4), xRadius: 2, yRadius: 2).fill()
    }
    NSGraphicsContext.restoreGraphicsState()

    // wide plush face
    let face = scout ? oval(11, 15, 42, 36) : NSBezierPath(roundedRect: NSRect(x: 12, y: 15, width: 40, height: 35), xRadius: 16, yRadius: 15)
    skin.setFill(); face.fill(); skinEdge.setStroke(); face.lineWidth = 0.8; face.stroke()

    // eyes: large, round, thick upper lash, glossy blue iris
    for (cx, dir): (CGFloat, CGFloat) in [(23.5, -1), (40.5, 1)] {
        let eye = NSBezierPath(roundedRect: NSRect(x: cx - 5, y: 25.5, width: 10, height: 10), xRadius: 4.8, yRadius: 4.8)
        NSColor.white.setFill(); eye.fill()
        NSGraphicsContext.saveGraphicsState(); eye.addClip()
        let iris = oval(cx - 4.3, 25.5, 8.6, 9.6)
        NSGradient(starting: scout ? rgb(72, 40, 22) : rgb(15, 70, 160), ending: scout ? rgb(186, 120, 62) : rgb(80, 185, 245))!
            .draw(in: iris, angle: -90)
        (scout ? rgb(60, 32, 18) : rgb(10, 45, 110)).setStroke(); iris.lineWidth = 0.7; iris.stroke()
        (scout ? rgb(45, 22, 12, 0.85) : rgb(15, 40, 95, 0.8)).setFill(); oval(cx - 2, 28.5, 4, 4.4).fill()
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

    if scout {
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

    // round glasses with a glint, bridge and arms
    for cx: CGFloat in [23.5, 40.5] {
        let lens = NSBezierPath(roundedRect: NSRect(x: cx - 6.3, y: 24.2, width: 12.6, height: 12.4), xRadius: 5.8, yRadius: 5.8)
        NSColor.white.withAlphaComponent(0.12).setFill(); lens.fill()
        rgb(60, 40, 30).setStroke(); lens.lineWidth = 1.25; lens.stroke()
        let glint = NSBezierPath(); glint.move(to: NSPoint(x: cx + 1.5, y: 35)); glint.line(to: NSPoint(x: cx + 4.5, y: 32))
        NSColor.white.withAlphaComponent(0.75).setStroke(); glint.lineWidth = 1.1; glint.lineCapStyle = .round; glint.stroke()
    }
    let frame = NSBezierPath()
    for (a, c): ((CGFloat, CGFloat), (CGFloat, CGFloat)) in [((29.2, 31.5), (34.8, 31.5)), ((17.2, 32), (11.5, 33.5)), ((46.8, 32), (52.5, 33.5))] {
        frame.move(to: NSPoint(x: a.0, y: a.1)); frame.line(to: NSPoint(x: c.0, y: c.1))
    }
    rgb(60, 40, 30).setStroke(); frame.lineWidth = 1.25; frame.lineCapStyle = .round; frame.stroke()

    // excited open grin and rosy cheeks
    let grin = NSBezierPath(); grin.move(to: NSPoint(x: 28.3, y: 22.6))
    grin.curve(to: NSPoint(x: 35.7, y: 22.6), controlPoint1: NSPoint(x: 29, y: 16.8), controlPoint2: NSPoint(x: 35, y: 16.8))
    grin.close(); rgb(130, 40, 45).setFill(); grin.fill()
    NSGraphicsContext.saveGraphicsState(); grin.addClip(); rgb(240, 120, 130).setFill(); oval(29.5, 17.5, 5, 3).fill()
    NSGraphicsContext.restoreGraphicsState()
    ink.setStroke(); grin.lineWidth = 0.8; grin.lineJoinStyle = .round; grin.stroke()
    rgb(250, 130, 130, 0.35).setFill(); oval(13.5, 21, 6, 3.4).fill(); oval(44.5, 21, 6, 3.4).fill()

    } else {
        // swept-back hair with a side part and a high hairline; short sideburns
        let top = NSBezierPath(); top.move(to: NSPoint(x: 8, y: 40))
        top.curve(to: NSPoint(x: 32, y: 62), controlPoint1: NSPoint(x: 7, y: 56), controlPoint2: NSPoint(x: 18, y: 62))
        top.curve(to: NSPoint(x: 56, y: 40), controlPoint1: NSPoint(x: 46, y: 62), controlPoint2: NSPoint(x: 57, y: 56))
        for (x, y): (CGFloat, CGFloat) in [(54, 30), (50, 32), (49, 43)] { top.line(to: NSPoint(x: x, y: y)) }
        top.curve(to: NSPoint(x: 15, y: 43), controlPoint1: NSPoint(x: 40, y: 49), controlPoint2: NSPoint(x: 24, y: 49))
        for (x, y): (CGFloat, CGFloat) in [(14, 32), (10, 30)] { top.line(to: NSPoint(x: x, y: y)) }
        top.close(); top.lineJoinStyle = .round
        hair.setFill(); top.fill(); hairDark.setStroke(); top.lineWidth = 0.9; top.stroke()
        let strands: [(CGFloat, CGFloat, CGFloat, CGFloat, CGFloat, CGFloat, CGFloat, CGFloat)] = [
            (29, 59, 49, 52, 36, 61, 44, 58), (27, 54, 51, 44, 34, 57, 44, 52), (25, 50.5, 50, 38, 33, 52, 43, 47),
            (22, 58, 10, 48, 15, 59, 10, 54), (21, 52, 11, 41, 15, 53, 11, 47)]
        for (x0, y0, x1, y1, c1x, c1y, c2x, c2y) in strands {
            let st = NSBezierPath(); st.move(to: NSPoint(x: x0, y: y0))
            st.curve(to: NSPoint(x: x1, y: y1), controlPoint1: NSPoint(x: c1x, y: c1y), controlPoint2: NSPoint(x: c2x, y: c2y))
            hairDark.withAlphaComponent(0.6).setStroke(); st.lineWidth = 0.8; st.lineCapStyle = .round; st.stroke()
        }
        let part = NSBezierPath(); part.move(to: NSPoint(x: 23.5, y: 47))
        part.curve(to: NSPoint(x: 28, y: 61), controlPoint1: NSPoint(x: 23, y: 53), controlPoint2: NSPoint(x: 25, y: 58))
        hairDark.setStroke(); part.lineWidth = 1.2; part.lineCapStyle = .round; part.stroke()
        let shine = NSBezierPath(); shine.move(to: NSPoint(x: 33, y: 58.5))
        shine.curve(to: NSPoint(x: 45, y: 54), controlPoint1: NSPoint(x: 37, y: 60), controlPoint2: NSPoint(x: 42, y: 57.5))
        NSColor.white.withAlphaComponent(0.6).setStroke(); shine.lineWidth = 1.4; shine.lineCapStyle = .round; shine.stroke()
        // thick, flat, stern brows
        for (a, c): ((CGFloat, CGFloat), (CGFloat, CGFloat)) in [((17.5, 38.8), (28.5, 37.6)), ((46.5, 38.8), (35.5, 37.6))] {
            let br = NSBezierPath(); br.move(to: NSPoint(x: a.0, y: a.1)); br.line(to: NSPoint(x: c.0, y: c.1))
            rgb(120, 88, 36).setStroke(); br.lineWidth = 3; br.lineCapStyle = .round; br.stroke()
        }
        // small nose and chin dimple
        let nose = NSBezierPath(); nose.move(to: NSPoint(x: 32, y: 27)); nose.line(to: NSPoint(x: 33, y: 24.6))
        skinEdge.setStroke(); nose.lineWidth = 0.9; nose.lineCapStyle = .round; nose.stroke()
        let dimple = NSBezierPath(); dimple.move(to: NSPoint(x: 30.8, y: 18)); dimple.line(to: NSPoint(x: 33.2, y: 18))
        skinEdge.setStroke(); dimple.lineWidth = 0.8; dimple.lineCapStyle = .round; dimple.stroke()
    }

    if scout {
        // tan uniform jacket collar over a white shirt
        let jacket = NSBezierPath(roundedRect: NSRect(x: 14, y: 2, width: 36, height: 14), xRadius: 6, yRadius: 6)
        rgb(196, 160, 112).setFill(); jacket.fill(); rgb(150, 115, 72).setStroke(); jacket.lineWidth = 0.8; jacket.stroke()
        let shirt = NSBezierPath(); shirt.move(to: NSPoint(x: 26, y: 16)); shirt.line(to: NSPoint(x: 32, y: 6)); shirt.line(to: NSPoint(x: 38, y: 16)); shirt.close()
        NSColor.white.setFill(); shirt.fill(); rgb(150, 115, 72).setStroke(); shirt.lineWidth = 0.8; shirt.stroke()
        // magnifying glass held up beside the face
        NSGraphicsContext.saveGraphicsState(); sh.set()
        let handle = NSBezierPath(); handle.move(to: NSPoint(x: 45, y: 3)); handle.line(to: NSPoint(x: 51, y: 11.5))
        rgb(110, 70, 40).setStroke(); handle.lineWidth = 3.2; handle.lineCapStyle = .round; handle.stroke()
        let ring = oval(48, 10, 13, 13)
        rgb(170, 225, 250, 0.45).setFill(); ring.fill()
        NSGraphicsContext.restoreGraphicsState()
        rgb(196, 146, 66).setStroke(); ring.lineWidth = 2.2; ring.stroke()
        let glare = NSBezierPath(); glare.move(to: NSPoint(x: 51.5, y: 18.5))
        glare.curve(to: NSPoint(x: 55, y: 21), controlPoint1: NSPoint(x: 52, y: 20), controlPoint2: NSPoint(x: 53.5, y: 21))
        NSColor.white.withAlphaComponent(0.85).setStroke(); glare.lineWidth = 1.2; glare.lineCapStyle = .round; glare.stroke()
        for x: CGFloat in [10.5, 41.5] {
            let hand = oval(x, 1.5, 7, 7); skin.setFill(); hand.fill(); skinEdge.setStroke(); hand.lineWidth = 0.9; hand.stroke()
        }
        return
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
    var scout = false, frameName = "pet", badgeColor = NSColor.systemRed
    var grab = NSPoint.zero, moved = false
    var overdue = 0 { didSet { if overdue != oldValue { needsDisplay = true } } }  // badge count
    override func draw(_ r: NSRect) {
        // Fully transparent pixels let clicks fall through; a near-invisible fill makes the whole square clickable.
        NSColor.black.withAlphaComponent(0.01).setFill(); bounds.fill()
        NSGraphicsContext.saveGraphicsState(); drawPet(bounds, scout: scout); NSGraphicsContext.restoreGraphicsState()
        guard overdue > 0 else { return }
        let text = NSAttributedString(string: overdue > 99 ? "99+" : "\(overdue)", attributes: [
            .font: NSFont.boldSystemFont(ofSize: 15), .foregroundColor: NSColor.white])
        let w = max(26, text.size().width + 12), r = NSRect(x: bounds.maxX - w - 2, y: bounds.maxY - 28, width: w, height: 26)
        let pill = NSBezierPath(roundedRect: r, xRadius: 13, yRadius: 13)
        badgeColor.setFill(); pill.fill(); NSColor.white.setStroke(); pill.lineWidth = 2; pill.stroke()
        text.draw(at: NSPoint(x: r.midX - text.size().width / 2, y: r.midY - text.size().height / 2))
    }
    override func acceptsFirstMouse(for e: NSEvent?) -> Bool { true }
    override func mouseDown(with e: NSEvent) { grab = e.locationInWindow; moved = false }
    override func mouseDragged(with e: NSEvent) {
        moved = true
        let p = NSEvent.mouseLocation
        window?.setFrameOrigin(NSPoint(x: p.x - grab.x, y: p.y - grab.y))
    }
    override func mouseUp(with e: NSEvent) {
        if moved { window?.saveFrame(usingName: frameName) } else { onClick() }
    }
}

class App: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!, feedWindow: NSWindow!
    var pet: NSPanel!, scoutPet: NSPanel!
    var feedWeb: WKWebView!
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
        window.title = "Task Manager"
        window.minSize = NSSize(width: 820, height: 480)
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("main")
        window.isReleasedWhenClosed = false
        window.makeKeyAndOrderFront(nil)
        web.load(URLRequest(url: base))

        // Scout window: starts hidden, shown by the scout pet
        feedWeb = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        feedWeb.navigationDelegate = self
        feedWeb.uiDelegate = self
        feedWindow = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 760),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        feedWindow.title = "Scout"
        feedWindow.minSize = NSSize(width: 560, height: 400)
        feedWindow.contentView = feedWeb
        feedWindow.center()
        feedWindow.setFrameAutosaveName("feed")
        feedWindow.isReleasedWhenClosed = false
        feedWeb.load(URLRequest(url: feedURL))

        let view = PetView()
        view.toolTip = "Task Manager: click to show/hide, drag to move"
        view.onClick = { [unowned self] in self.toggle(self.window) }
        pet = makePet(view, offset: 26)
        let scoutView = PetView()
        scoutView.scout = true; scoutView.frameName = "scoutPet"; scoutView.badgeColor = .systemBlue
        scoutView.toolTip = "Scout: new papers, videos and repos. Click to show/hide, drag to move"
        scoutView.onClick = { [unowned self] in self.toggle(self.feedWindow) }
        scoutPet = makePet(scoutView, offset: 26 + petSize + 8)

        // red badge on the pet: open, not-done tasks whose due time has passed
        let refresh = { [weak view] in
            URLSession.shared.dataTask(with: base.appendingPathComponent("api/todos")) { data, _, _ in
                guard let data, let list = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return }
                let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = "yyyy-MM-dd'T'HH:mm"
                let now = f.string(from: Date())
                let n = list.filter { ($0["done"] as? Bool) != true && !((($0["due"] as? String) ?? "").isEmpty) && ($0["due"] as! String) <= now }.count
                DispatchQueue.main.async { view?.overdue = n }
            }.resume()
        }
        // blue badge on the scout pet: unread picks (score 7+, not thumbed down)
        let refreshScout = { [weak scoutView] in
            URLSession.shared.dataTask(with: base.appendingPathComponent("api/feed")) { data, _, _ in
                guard let data, let f = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let items = f["items"] as? [[String: Any]] else { return }
                let n = items.filter { ($0["read"] as? Bool) != true && (($0["vote"] as? Int) ?? 0) >= 0
                                       && (($0["score"] as? Double) ?? 0) >= 7 }.count
                DispatchQueue.main.async { scoutView?.overdue = n }
            }.resume()
        }
        Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { _ in refresh(); refreshScout() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { refresh(); refreshScout() }
    }

    func makePet(_ view: PetView, offset: CGFloat) -> NSPanel {
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: petSize, height: petSize),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.level = .floating
        p.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
        p.backgroundColor = .clear
        p.isOpaque = false
        p.hasShadow = false
        p.contentView = view
        if !p.setFrameUsingName(view.frameName), let f = NSScreen.main?.visibleFrame {
            p.setFrameOrigin(NSPoint(x: f.maxX - petSize - offset, y: f.minY + 20))  // bottom-right corner
        }
        p.setContentSize(NSSize(width: petSize, height: petSize))  // saved frame may hold an old size
        p.orderFrontRegardless()
        return p
    }

    func toggle(_ w: NSWindow) {
        if w.isVisible && w.isKeyWindow && NSApp.isActive {
            w.orderOut(nil)
        } else {
            NSApp.activate(ignoringOtherApps: true)
            w.makeKeyAndOrderFront(nil)
        }
    }

    // server takes a moment to start; keep retrying
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { w.load(URLRequest(url: w === self.feedWeb ? feedURL : base)) }
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
appMenu.addItem(withTitle: "Quit Task Manager", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
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
