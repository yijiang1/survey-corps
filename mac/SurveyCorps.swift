// Native window around app.py. Handles Outlook drags itself (file promises), which browsers don't reliably do.
import Cocoa
import WebKit

// app.py is found relative to this source file, so rebuild if you move the project.
let appPy = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
    .appendingPathComponent("app.py").path
let base = URL(string: "http://127.0.0.1:8765")!
let feedURL = base.appendingPathComponent("feed")
let libURL = base.appendingPathComponent("library")
let settingsURL = base.appendingPathComponent("settings")
let petSize: CGFloat = 112  // pet panel size in points: a 96 pt pet plus room to hop and lean

// POST to app.py; done runs on the main thread with the body and status (0 = server unreachable).
func post(_ path: String, _ body: Any? = nil, raw: Data? = nil, query: [URLQueryItem] = [],
          done: @escaping (Data?, Int) -> Void = { _, _ in }) {
    var c = URLComponents(url: base.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
    if !query.isEmpty { c.queryItems = query }
    var req = URLRequest(url: c.url!)
    req.httpMethod = "POST"
    req.timeoutInterval = 150  // Argo answers can take a minute or two
    req.httpBody = raw ?? body.flatMap { try? JSONSerialization.data(withJSONObject: $0) }
    URLSession.shared.dataTask(with: req) { data, resp, _ in
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        DispatchQueue.main.async { done(data, code) }
    }.resume()
}
func json(_ d: Data?) -> [String: Any] { d.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:] }

// Dropped files: real file URLs, or Outlook's file promises (written to a temp folder first).
func receiveFiles(_ pb: NSPasteboard, each: @escaping (URL) -> Void) {
    if let urls = pb.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL], !urls.isEmpty {
        urls.forEach(each)
    } else if let promises = pb.readObjects(forClasses: [NSFilePromiseReceiver.self]) as? [NSFilePromiseReceiver] {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        for p in promises {
            p.receivePromisedFiles(atDestination: dir, options: [:], operationQueue: .main) { url, err in
                if err == nil { each(url) }
            }
        }
    }
}

// An email (or any file) becomes a task; done gets its title, nil on failure.
func uploadEmail(_ file: URL, done: @escaping (String?) -> Void = { _ in }) {
    guard let data = try? Data(contentsOf: file) else { return done(nil) }
    post("api/drop", raw: data, query: [URLQueryItem(name: "name", value: file.lastPathComponent)]) { d, code in
        done(code == 200 ? json(d)["title"] as? String ?? file.lastPathComponent : nil)
    }
}

// One-line text box in an alert. nil when cancelled or left blank.
func textPrompt(_ title: String, info: String = "", text: String = "", placeholder: String = "") -> String? {
    NSApp.activate(ignoringOtherApps: true)  // pets don't activate the app; the alert needs it
    let a = NSAlert(); a.messageText = title; a.informativeText = info
    a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
    let tf = NSTextField(frame: NSRect(x: 0, y: 0, width: 340, height: 24))
    tf.stringValue = text; tf.placeholderString = placeholder
    a.accessoryView = tf
    a.window.initialFirstResponder = tf
    guard a.runModal() == .alertFirstButtonReturn else { return nil }
    let v = tf.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
    return v.isEmpty ? nil : v
}

class DropWebView: WKWebView {
    var library = false  // Library window: Finder drops add the paths themselves, not the files' contents
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
        if library {
            let urls = pb.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
            post("api/library/add", ["paths": urls.map(\.path)]) { data, _ in
                let json = data.flatMap { String(data: $0, encoding: .utf8) } ?? "{\"added\":[],\"skipped\":[]}"
                self.evaluateJavaScript("dropped(\(json))")
            }
            return true
        }
        receiveFiles(pb) { uploadEmail($0) { _ in self.evaluateJavaScript("drop.classList.remove('over');load()") } }
        return true
    }
}

// Original plush-chibi pet: swept-back blond hair, thick brows, square jaw, stern look, holding a letter. 64x64 box.
// librarian: the third pet (Library window) — Armin-style: blond bob with a fringe, big blue eyes, holding an open book.
// scout: the second pet (Scout window) — Hange-style researcher: messy brown ponytail, glasses, excited grin, magnifying glass.
func drawPet(_ b: NSRect, scout: Bool = false, librarian: Bool = false) {
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
    let back = NSBezierPath(roundedRect: librarian ? NSRect(x: 6, y: 15, width: 52, height: 47) : NSRect(x: 7, y: 20, width: 50, height: 42),
                            xRadius: 23, yRadius: 22)
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

    } else if librarian {
        // bob: rounded crown, a fringe of soft points down to the brows, side locks to the jaw
        let bob = NSBezierPath(); bob.move(to: NSPoint(x: 8, y: 36))
        bob.curve(to: NSPoint(x: 32, y: 62), controlPoint1: NSPoint(x: 7, y: 55), controlPoint2: NSPoint(x: 18, y: 62))
        bob.curve(to: NSPoint(x: 56, y: 36), controlPoint1: NSPoint(x: 46, y: 62), controlPoint2: NSPoint(x: 57, y: 55))
        for (x, y): (CGFloat, CGFloat) in [(56, 19), (51.5, 17.5), (50.5, 37), (46, 41.5), (41.5, 38.5), (36.5, 42), (32, 38.5),
                                           (27.5, 42), (22.5, 38.5), (18, 41.5), (13.5, 37), (12.5, 17.5), (8, 19)] {
            bob.line(to: NSPoint(x: x, y: y))
        }
        bob.close(); bob.lineJoinStyle = .round
        hair.setFill(); bob.fill(); hairDark.setStroke(); bob.lineWidth = 0.9; bob.stroke()
        for (x0, y0, x1, y1): (CGFloat, CGFloat, CGFloat, CGFloat) in [(24, 57, 22.5, 41), (32, 59, 32, 41), (40, 57, 41.5, 41),
                                                                       (11, 44, 10.5, 22), (53, 44, 53.5, 22)] {
            let s = NSBezierPath(); s.move(to: NSPoint(x: x0, y: y0)); s.line(to: NSPoint(x: x1, y: y1))
            hairDark.withAlphaComponent(0.55).setStroke(); s.lineWidth = 0.7; s.lineCapStyle = .round; s.stroke()
        }
        let shine = NSBezierPath(); shine.move(to: NSPoint(x: 19, y: 56.5))
        shine.curve(to: NSPoint(x: 30, y: 60), controlPoint1: NSPoint(x: 22, y: 58.8), controlPoint2: NSPoint(x: 26, y: 60))
        NSColor.white.withAlphaComponent(0.65).setStroke(); shine.lineWidth = 1.5; shine.lineCapStyle = .round; shine.stroke()
        rgb(250, 140, 140, 0.3).setFill(); oval(14.5, 21, 6, 3.4).fill(); oval(43.5, 21, 6, 3.4).fill()
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

    if librarian {  // an open book, held with two little hands
        NSGraphicsContext.saveGraphicsState(); sh.set()
        let book = NSBezierPath(); book.move(to: NSPoint(x: 13, y: 4)); book.line(to: NSPoint(x: 32, y: 1.5)); book.line(to: NSPoint(x: 51, y: 4))
        book.line(to: NSPoint(x: 51, y: 17)); book.line(to: NSPoint(x: 32, y: 15)); book.line(to: NSPoint(x: 13, y: 17)); book.close()
        rgb(46, 110, 80).setFill(); book.fill()
        NSGraphicsContext.restoreGraphicsState()
        for side: CGFloat in [-1, 1] {
            let page = NSBezierPath(); page.move(to: NSPoint(x: 32, y: 3)); page.line(to: NSPoint(x: 32 + side * 17.5, y: 5.3))
            page.line(to: NSPoint(x: 32 + side * 17.5, y: 16.2)); page.line(to: NSPoint(x: 32, y: 14)); page.close()
            rgb(255, 250, 238).setFill(); page.fill(); rgb(200, 185, 160).setStroke(); page.lineWidth = 0.6; page.stroke()
            for y: CGFloat in [7.5, 10, 12.5] {
                let line = NSBezierPath(); line.move(to: NSPoint(x: 32 + side * 3, y: y)); line.line(to: NSPoint(x: 32 + side * 14, y: y + 1.3))
                rgb(150, 150, 165, 0.7).setStroke(); line.lineWidth = 0.7; line.stroke()
            }
        }
        for x: CGFloat in [9.5, 47.5] {
            let hand = oval(x, 5, 7, 7); skin.setFill(); hand.fill(); skinEdge.setStroke(); hand.lineWidth = 0.9; hand.stroke()
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

// Menu item that runs a closure.
class Item: NSMenuItem {
    var run: () -> Void = {}
    convenience init(_ title: String, enabled: Bool = true, _ run: @escaping () -> Void = {}) {
        self.init(title: title, action: #selector(fire), keyEquivalent: "")
        self.run = run; target = self; isEnabled = enabled
    }
    @objc func fire() { run() }
}

// Floating pet that stays on every desktop. Click: show/hide its window. Drag: move it. Drop things on it.
// Right-click: its menu. A "…" thought bubble while Argo works for it.
class PetView: NSView {
    var onClick: () -> Void = {}
    var onDrop: (NSPasteboard) -> Bool = { _ in false }
    var makeMenu: () -> NSMenu = { NSMenu() }
    var scout = false, librarian = false, frameName = "pet", badgeColor = NSColor.systemRed
    var grab = NSPoint.zero, moved = false
    var overdue = 0 { didSet { if overdue != oldValue { needsDisplay = true } } }  // badge count
    var review = 0 { didSet { if review != oldValue { needsDisplay = true } } }    // Argo fill-ins to Keep / Undo
    var hover = false { didSet { if hover != oldValue { needsDisplay = true } } }  // something dragged over it
    var busy = false { didSet { if busy != oldValue { animate() } } }
    enum Anim { case hop, shake }  // hop: a drop worked or the pet speaks; shake: it didn't
    var anim: Anim?, animStart = Date(), ticker: Timer?
    func play(_ a: Anim) { anim = a; animStart = Date(); animate() }
    func animate() {  // redraw at 30 fps while something moves, stop when nothing does
        needsDisplay = true
        guard ticker == nil, busy || anim != nil else { return }
        ticker = Timer.scheduledTimer(withTimeInterval: 1 / 30, repeats: true) { [weak self] t in
            guard let self else { return t.invalidate() }
            self.needsDisplay = true
            if !self.busy && self.anim == nil { t.invalidate(); self.ticker = nil }
        }
    }
    var reviewRect: NSRect { NSRect(x: bounds.maxX - 30, y: 2, width: 28, height: 22) }
    override func draw(_ r: NSRect) {
        // Fully transparent pixels let clicks fall through; a near-invisible fill makes the whole square clickable.
        NSColor.black.withAlphaComponent(0.01).setFill(); bounds.fill()
        // pose: lean in while something is dragged over, hop / shake once, sway while busy
        let now = Date().timeIntervalSinceReferenceDate, t = Date().timeIntervalSince(animStart)
        var dx: CGFloat = 0, dy: CGFloat = 0, turn: CGFloat = 0, grow: CGFloat = hover ? 1.08 : 1
        if let a = anim {
            if t > 0.5 { anim = nil }
            else if a == .hop { dy = 9 * sin(t / 0.5 * .pi) }
            else { dx = 5 * sin(t * 45) * (1 - t / 0.5) }
        }
        if busy { turn = 4 * sin(now * 3.5) }
        let pet = NSRect(x: (bounds.width - 96) / 2, y: 3, width: 96, height: 96)
        NSGraphicsContext.saveGraphicsState()
        let cg = NSGraphicsContext.current!.cgContext
        cg.translateBy(x: pet.midX + dx, y: pet.minY + dy)  // pivot at the feet
        cg.rotate(by: turn * .pi / 180); cg.scaleBy(x: grow, y: grow)
        cg.translateBy(x: -48, y: 0)
        drawPet(NSRect(x: 0, y: 0, width: 96, height: 96), scout: scout, librarian: librarian)
        NSGraphicsContext.restoreGraphicsState()
        let phase = Int(now / 0.35)
        if hover {
            let ring = NSBezierPath(roundedRect: bounds.insetBy(dx: 2, dy: 2), xRadius: 20, yRadius: 20)
            NSColor.controlAccentColor.setStroke(); ring.lineWidth = 3; ring.stroke()
        }
        if busy {
            let t = NSRect(x: 3, y: bounds.maxY - 25, width: 34, height: 20)
            let b = NSBezierPath(roundedRect: t, xRadius: 10, yRadius: 10)
            b.appendOval(in: NSRect(x: t.minX + 15, y: t.minY - 6, width: 5, height: 5))  // little thought puff below
            NSColor.white.withAlphaComponent(0.95).setFill(); b.fill(); NSColor.gray.setStroke(); b.lineWidth = 1; b.stroke()
            for i in 0..<3 {
                (phase % 3 == i ? NSColor.darkGray : NSColor(white: 0.78, alpha: 1)).setFill()
                NSBezierPath(ovalIn: NSRect(x: t.minX + 7 + CGFloat(i) * 8, y: t.midY - 2.5, width: 5, height: 5)).fill()
            }
        }
        if review > 0 {
            let pill = NSBezierPath(roundedRect: reviewRect, xRadius: 11, yRadius: 11)
            NSColor.systemOrange.setFill(); pill.fill(); NSColor.white.setStroke(); pill.lineWidth = 2; pill.stroke()
            let s = NSAttributedString(string: "✓\(review > 9 ? "9+" : "\(review)")", attributes: [
                .font: NSFont.boldSystemFont(ofSize: 11), .foregroundColor: NSColor.white])
            s.draw(at: NSPoint(x: reviewRect.midX - s.size().width / 2, y: reviewRect.midY - s.size().height / 2))
        }
        guard overdue > 0 else { return }
        let text = NSAttributedString(string: overdue > 99 ? "99+" : "\(overdue)", attributes: [
            .font: NSFont.boldSystemFont(ofSize: 15), .foregroundColor: NSColor.white])
        let w = max(26, text.size().width + 12), r = NSRect(x: bounds.maxX - w - 2, y: bounds.maxY - 28, width: w, height: 26)
        let pill = NSBezierPath(roundedRect: r, xRadius: 13, yRadius: 13)
        badgeColor.setFill(); pill.fill(); NSColor.white.setStroke(); pill.lineWidth = 2; pill.stroke()
        text.draw(at: NSPoint(x: r.midX - text.size().width / 2, y: r.midY - text.size().height / 2))
    }
    override func acceptsFirstMouse(for e: NSEvent?) -> Bool { true }
    override func rightMouseDown(with e: NSEvent) { NSMenu.popUpContextMenu(makeMenu(), with: e, for: self) }
    override func mouseDown(with e: NSEvent) {
        grab = e.locationInWindow; moved = false
        if e.modifierFlags.contains(.control) { moved = true; rightMouseDown(with: e) }  // ctrl-click = right-click
    }
    override func mouseDragged(with e: NSEvent) {
        moved = true
        let p = NSEvent.mouseLocation
        window?.setFrameOrigin(NSPoint(x: p.x - grab.x, y: p.y - grab.y))
    }
    override func mouseUp(with e: NSEvent) {
        if moved { window?.saveFrame(usingName: frameName) }
        else if review > 0 && reviewRect.contains(convert(e.locationInWindow, from: nil)) { rightMouseDown(with: e) }
        else { onClick() }
    }
    static let dragTypes = DropWebView.promiseTypes + [.fileURL, .URL, .string]
    override func draggingEntered(_ s: NSDraggingInfo) -> NSDragOperation { hover = true; return .copy }
    override func draggingExited(_ s: NSDraggingInfo?) { hover = false }
    override func performDragOperation(_ s: NSDraggingInfo) -> Bool { hover = false; return onDrop(s.draggingPasteboard) }
}

// Speech bubble above a pet: briefings, Scout picks, answers, routine results. Click: open the pet's window.
class Bubble: NSPanel {
    var onClick: () -> Void = {}
    var hideTimer: Timer?
    let label = NSTextField(wrappingLabelWithString: "")
    let view = BubbleView()
    init() {
        super.init(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
        backgroundColor = .clear; isOpaque = false; hasShadow = true
        view.bubble = self
        view.addSubview(label)
        contentView = view
    }
    func show(_ title: String, _ text: String, over pet: NSWindow) {
        let t = NSMutableAttributedString(string: title.isEmpty ? "" : title + "\n",
                                          attributes: [.font: NSFont.boldSystemFont(ofSize: 12.5), .foregroundColor: NSColor.labelColor])
        t.append(NSAttributedString(string: String(text.prefix(700)) + (text.count > 700 ? "…" : ""),
                                    attributes: [.font: NSFont.systemFont(ofSize: 12.5), .foregroundColor: NSColor.labelColor]))
        label.attributedStringValue = t
        let s = label.sizeThatFits(NSSize(width: 280, height: 2000)), tail = BubbleView.tail
        label.frame = NSRect(x: 12, y: tail + 9, width: s.width, height: s.height)
        let w = s.width + 24, h = s.height + 18 + tail
        let screen = (pet.screen ?? NSScreen.main!).visibleFrame
        let x = min(max(pet.frame.midX - w / 2, screen.minX + 4), screen.maxX - w - 4)
        let y = min(pet.frame.maxY - 8, screen.maxY - h)
        view.tailX = min(max(pet.frame.midX - x, 18), w - 18)
        setFrame(NSRect(x: x, y: y, width: w, height: h), display: true)
        view.needsDisplay = true
        orderFrontRegardless()
        hideTimer?.invalidate()
        hideTimer = Timer.scheduledTimer(withTimeInterval: min(45, 6 + Double(t.length) * 0.05), repeats: false) { [weak self] _ in
            self?.orderOut(nil)
        }
    }
}

class BubbleView: NSView {
    static let tail: CGFloat = 10
    weak var bubble: Bubble?
    var tailX: CGFloat = 40
    override func draw(_ r: NSRect) {
        let t = Self.tail, body = NSRect(x: 0.5, y: t + 0.5, width: bounds.width - 1, height: bounds.height - t - 1)
        let p = NSBezierPath(roundedRect: body, xRadius: 12, yRadius: 12)
        p.move(to: NSPoint(x: tailX - 8, y: t + 1)); p.line(to: NSPoint(x: tailX, y: 0.5)); p.line(to: NSPoint(x: tailX + 8, y: t + 1))
        NSColor.windowBackgroundColor.setFill(); p.fill()
        NSColor.separatorColor.setStroke(); p.lineWidth = 1; p.stroke()
        NSColor.windowBackgroundColor.setFill(); NSRect(x: tailX - 7, y: t, width: 14, height: 2).fill()  // seam between body and tail
    }
    override func hitTest(_ p: NSPoint) -> NSView? { frame.contains(p) ? self : nil }
    override func acceptsFirstMouse(for e: NSEvent?) -> Bool { true }
    override func mouseUp(with e: NSEvent) { bubble?.orderOut(nil); bubble?.onClick() }
}

class App: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    var window: NSWindow!, feedWindow: NSWindow!, libWindow: NSWindow!, settingsWindow: NSWindow!
    var pets: [String: (panel: NSPanel, view: PetView, window: NSWindow)] = [:]  // "task", "scout", "library"
    var bubbles: [String: Bubble] = [:], lastBubble = 0
    var review: [[String: Any]] = [], routines: [[String: String]] = []
    var feedWeb: WKWebView!, libWeb: DropWebView!, settingsWeb: WKWebView!
    let server = Process()

    @objc func openSettings() {  // Survey Corps > Settings… (⌘,): its own window, like any Mac app
        if settingsWindow == nil {
            settingsWeb = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
            settingsWeb.navigationDelegate = self
            settingsWindow = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 540),
                                      styleMask: [.titled, .closable], backing: .buffered, defer: false)
            settingsWindow.title = "Settings"
            settingsWindow.contentView = settingsWeb
            settingsWindow.center()
            settingsWindow.setFrameAutosaveName("settings")
            settingsWindow.isReleasedWhenClosed = false
            settingsWeb.load(URLRequest(url: settingsURL))
        }
        settingsWindow.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

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

        // Library window: starts hidden, shown by the librarian pet
        let libConfig = WKWebViewConfiguration()
        libConfig.userContentController.add(self, name: "choose")  // the page's "Choose in Finder…" button
        libWeb = DropWebView(frame: .zero, configuration: libConfig)
        libWeb.library = true
        libWeb.navigationDelegate = self
        libWeb.uiDelegate = self
        libWindow = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 760),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        libWindow.title = "Library"
        libWindow.minSize = NSSize(width: 720, height: 420)
        libWindow.contentView = libWeb
        libWindow.center()
        libWindow.setFrameAutosaveName("library")
        libWindow.isReleasedWhenClosed = false
        libWeb.load(URLRequest(url: libURL))

        for (i, (kind, w, tip)) in [("task", window!, "Task Manager"), ("scout", feedWindow!, "Scout: new papers, videos and repos"),
                                    ("library", libWindow!, "Library: your projects, files and docs")].enumerated() {
            let v = PetView()
            v.scout = kind == "scout"; v.librarian = kind == "library"
            v.frameName = ["task": "pet", "scout": "scoutPet", "library": "libPet"][kind]!
            if kind == "scout" { v.badgeColor = .systemBlue }
            v.toolTip = tip + ". Click to show/hide, drag to move, drop things on it, right-click for more"
            v.onClick = { [unowned self] in self.toggle(w) }
            v.onDrop = { [unowned self] in self.drop(kind, $0) }
            v.makeMenu = { [unowned self] in self.menu(kind) }
            pets[kind] = (makePet(v, offset: 26 + CGFloat(i) * (petSize + 4)), v, w)
        }
        Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [unowned self] _ in self.poll() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.poll() }
    }

    // Everything the pets show comes from one call: badges, who's busy, review queue, new speech bubbles.
    func poll() {
        URLSession.shared.dataTask(with: URL(string: "\(base)/api/pets?after=\(lastBubble)")!) { data, _, _ in
            let s = json(data)
            guard !s.isEmpty else { return }
            DispatchQueue.main.async {
                let busy = s["busy"] as? [String: Bool] ?? [:]
                self.review = s["review"] as? [[String: Any]] ?? []
                self.routines = s["routines"] as? [[String: String]] ?? []
                for (k, p) in self.pets { p.view.busy = busy[k] ?? false }
                self.pets["task"]?.view.overdue = s["overdue"] as? Int ?? 0  // red: open tasks past their due time
                self.pets["task"]?.view.review = self.review.count           // orange: Argo fill-ins to Keep / Undo
                self.pets["scout"]?.view.overdue = s["picks"] as? Int ?? 0   // blue: unread picks (score 7+, not thumbed down)
                for b in s["bubbles"] as? [[String: Any]] ?? [] {
                    self.lastBubble = max(self.lastBubble, b["id"] as? Int ?? 0)
                    self.say(b["pet"] as? String ?? "task", b["title"] as? String ?? "", b["text"] as? String ?? "")
                }
            }
        }.resume()
    }

    func say(_ kind: String, _ title: String, _ text: String, ok: Bool = true) {
        guard let p = pets[kind] else { return }
        let b = bubbles[kind] ?? Bubble()
        bubbles[kind] = b
        b.onClick = { NSApp.activate(ignoringOtherApps: true); p.window.makeKeyAndOrderFront(nil) }
        b.show(title, text, over: p.panel)
        p.view.play(ok ? .hop : .shake)
    }

    // Drop onto a pet: emails/text -> a task, a link -> Scout's Saved, files/folders -> the Library.
    func drop(_ kind: String, _ pb: NSPasteboard) -> Bool {
        let files = pb.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
        let text = pb.string(forType: .string)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        switch kind {
        case "library":
            guard !files.isEmpty else { say(kind, "", "Drop files or folders from Finder on me", ok: false); return false }
            addToLibrary(files.map(\.path))
        case "scout":
            let link = (pb.readObjects(forClasses: [NSURL.self]) as? [URL])?.first { !$0.isFileURL }?.absoluteString ?? text
            guard link.hasPrefix("http") else { say(kind, "", "Drop a link on me: arXiv, YouTube, GitHub or any page", ok: false); return false }
            saveLink(link)
        default:
            if !files.isEmpty || pb.types?.contains(where: DropWebView.promiseTypes.contains) == true {
                receiveFiles(pb) { uploadEmail($0) { t in self.say("task", "", t.map { "Added: \($0)" } ?? "Couldn't read that file", ok: t != nil) } }
            } else if !text.isEmpty {
                addTask(text)
            } else { return false }
        }
        return true
    }

    func addTask(_ text: String) {
        post("api/drop", raw: Data(text.utf8)) { d, code in
            self.say("task", "", code == 200 ? "Added: \(json(d)["title"] as? String ?? text)" : "Couldn't add that task", ok: code == 200)
        }
    }

    func saveLink(_ link: String) {
        post("api/feed/add", ["url": link]) { d, code in
            let r = json(d), title = r["title"] as? String ?? link
            self.say("scout", "", code == 200 ? (r["existing"] as? Bool == true ? "Already had it; saved: " : "Saved: ") + title
                                              : r["error"] as? String ?? "Couldn't save that link", ok: code == 200)
        }
    }

    func addToLibrary(_ paths: [String]) {
        post("api/library/add", ["paths": paths]) { d, code in
            let r = json(d), added = r["added"] as? [String] ?? [], skipped = r["skipped"] as? [String] ?? []
            let msg = (added.isEmpty ? [] : ["Added: " + added.joined(separator: ", ")]) + skipped
            self.say("library", "", code == 200 ? msg.joined(separator: "\n") : "Couldn't add that", ok: code == 200 && !added.isEmpty)
        }
    }

    // Question -> Argo -> the pet's speech bubble. The pet shows "…" while Argo thinks (the poll keeps it in sync).
    func ask(_ kind: String, _ path: String, _ title: String, _ example: String) {
        guard let q = textPrompt(title, placeholder: example) else { return }
        pets[kind]?.view.busy = true
        post(path, ["q": q]) { d, code in
            self.pets[kind]?.view.busy = false
            self.say(kind, q, code == 200 ? json(d)["answer"] as? String ?? "" : "Argo is unavailable right now (down, or off VPN).", ok: code == 200)
        }
    }

    func reviewItem(_ r: [String: Any]) -> NSMenuItem {
        let id = r["id"] as? String ?? "", labels = r["labels"] as? [String] ?? [], due = r["due"] as? String ?? ""
        var what = labels.isEmpty ? [] : ["labels " + labels.joined(separator: ", ")]
        if !due.isEmpty { what.append("due " + due.replacingOccurrences(of: "T", with: " ")) }
        if r["renamed"] as? Bool == true { what.append("renamed") }
        if let n = r["actions"] as? Int, n > 0 { what.append("\(n) action items") }
        let title = r["title"] as? String ?? ""
        let item = NSMenuItem(title: (title.count > 44 ? title.prefix(43) + "…" : title) + "  —  " + what.joined(separator: "; "),
                              action: nil, keyEquivalent: "")
        let sub = NSMenu()
        sub.addItem(Item("Keep") { post("api/todo/\(id)/keep") { _, _ in self.poll() } })
        sub.addItem(Item("Undo Argo's Changes") { post("api/todo/\(id)/undo") { _, _ in self.poll() } })
        item.submenu = sub
        return item
    }

    func menu(_ kind: String) -> NSMenu {
        let m = NSMenu(), p = pets[kind]!
        m.autoenablesItems = false
        m.addItem(Item((p.window.isVisible ? "Hide " : "Show ") + p.window.title) { self.toggle(p.window) })
        m.addItem(.separator())
        switch kind {
        case "task":
            m.addItem(Item("Quick Add Task…") {
                if let v = textPrompt("New task", info: "With Argo, plain language works.", placeholder: "review Bob's draft by Fri 3pm") { self.addTask(v) }
            })
            m.addItem(Item("Ask About My Tasks…") { self.ask(kind, "api/ask", "Ask about your tasks and emails", "what did Bob ask me about the beamtime?") })
            if !review.isEmpty {
                m.addItem(.separator())
                m.addItem(Item("Argo filled in:", enabled: false))
                review.forEach { m.addItem(reviewItem($0)) }
                m.addItem(Item("Keep All") { for r in self.review { post("api/todo/\(r["id"] as? String ?? "")/keep") { _, _ in self.poll() } } })
            }
        case "scout":
            m.addItem(Item("Check for New Now") { post("api/feed/refresh") { _, _ in self.poll() } })
            m.addItem(Item("Save a Link…") { if let v = textPrompt("Save a link to Scout", placeholder: "https://arxiv.org/abs/…") { self.saveLink(v) } })
            m.addItem(Item("Mark All Read") { post("api/feed/update", ["all_read": true]) { _, _ in self.poll() } })
        default:
            m.addItem(Item("Find in Library…") { self.ask(kind, "api/library/ask", "Ask about your library", "where's my beamtime analysis?") })
            m.addItem(Item("Add Files or Folders…") {
                NSApp.activate(ignoringOtherApps: true)
                let o = NSOpenPanel()
                o.canChooseFiles = true; o.canChooseDirectories = true; o.allowsMultipleSelection = true
                if o.runModal() == .OK { self.addToLibrary(o.urls.map(\.path)) }
            })
        }
        m.addItem(.separator())
        let mine = routines.filter { $0["pet"] == kind }
        if !mine.isEmpty {
            let run = NSMenuItem(title: "Run Routine Now", action: nil, keyEquivalent: ""), sub = NSMenu()
            for r in mine { sub.addItem(Item(r["line"] ?? "") { post("api/routines/run", ["line": r["line"] ?? ""]) { _, _ in self.poll() } }) }
            run.submenu = sub
            m.addItem(run)
        }
        m.addItem(Item("Edit Routines…") { self.openSettings() })
        return m
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
        view.registerForDraggedTypes(PetView.dragTypes)
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

    // Library "Choose in Finder…": the standard Open panel (files and folders, several at once, New Folder for a new project)
    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        let p = NSOpenPanel()
        p.canChooseFiles = true; p.canChooseDirectories = true; p.allowsMultipleSelection = true; p.canCreateDirectories = true
        p.prompt = "Choose"; p.message = "Pick projects, files or docs (⌘-click for several). New Folder starts a new project."
        p.beginSheetModal(for: libWindow) { r in
            guard r == .OK, let data = try? JSONSerialization.data(withJSONObject: p.urls.map(\.path)),
                  let json = String(data: data, encoding: .utf8) else { return }
            self.libWeb.evaluateJavaScript("chosen(\(json))")
        }
    }

    // server takes a moment to start; keep retrying
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { w.load(URLRequest(url: w === self.feedWeb ? feedURL : w === self.libWeb ? libURL : w === self.settingsWeb ? settingsURL : base)) }
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
        done(textPrompt(p, text: d ?? ""))
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate

// Menus: Cmd+Q/W, plus Edit so Cmd+C/V/Z work in text fields.
let mainMenu = NSMenu()
let appMenu = NSMenu()
let settingsItem = NSMenuItem(title: "Settings…", action: #selector(App.openSettings), keyEquivalent: ",")
settingsItem.target = delegate
appMenu.addItem(settingsItem)
appMenu.addItem(.separator())
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
