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
let leviURL = base.appendingPathComponent("levi")
let petSize: CGFloat = 112  // pet panel size in points: a 96 pt pet plus room to hop and lean
let sliver: CGFloat = 40    // how much of a tucked pet stays on screen, drawn as a tab

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
        return types.contains(.fileURL) || types.contains(where: Self.promiseTypes.contains) || (library && types.contains(.URL))
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
            let urls = pb.readObjects(forClasses: [NSURL.self]) as? [URL] ?? []  // files, or links dragged from a browser
            post("api/library/add", ["paths": urls.map { $0.isFileURL ? $0.path : $0.absoluteString }]) { data, _ in
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
// levi: the fourth pet (coding agents) — Levi-style: black undercut, centre-parted fringe, narrow steel eyes, holding a laptop.
func drawPet(_ b: NSRect, scout: Bool = false, librarian: Bool = false, levi: Bool = false) {
    let k = min(b.width, b.height) / 64
    NSGraphicsContext.current?.cgContext.scaleBy(x: k, y: k)
    func rgb(_ r: CGFloat, _ g: CGFloat, _ bl: CGFloat, _ a: CGFloat = 1) -> NSColor {
        NSColor(srgbRed: r / 255, green: g / 255, blue: bl / 255, alpha: a)
    }
    func oval(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) -> NSBezierPath {
        NSBezierPath(ovalIn: NSRect(x: x, y: y, width: w, height: h))
    }
    let hair = levi ? rgb(40, 38, 46) : scout ? rgb(128, 84, 52) : rgb(250, 226, 130)
    let hairDark = levi ? rgb(14, 13, 18) : scout ? rgb(82, 50, 28) : rgb(214, 178, 72), skin = rgb(248, 216, 192), skinEdge = rgb(222, 180, 152)
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
    let back = NSBezierPath(roundedRect: librarian ? NSRect(x: 6, y: 15, width: 52, height: 47)
                            : levi ? NSRect(x: 8, y: 30, width: 48, height: 32) : NSRect(x: 7, y: 20, width: 50, height: 42),
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
        let eye = NSBezierPath(roundedRect: NSRect(x: cx - 5, y: levi ? 26.5 : 25.5, width: 10, height: levi ? 6.5 : 10),
                               xRadius: levi ? 3 : 4.8, yRadius: levi ? 3 : 4.8)
        NSColor.white.setFill(); eye.fill()
        NSGraphicsContext.saveGraphicsState(); eye.addClip()
        let iris = oval(cx - 4.3, 25.5, 8.6, 9.6)
        NSGradient(starting: levi ? rgb(55, 62, 76) : scout ? rgb(72, 40, 22) : rgb(15, 70, 160),
                   ending: levi ? rgb(150, 162, 178) : scout ? rgb(186, 120, 62) : rgb(80, 185, 245))!
            .draw(in: iris, angle: -90)
        (scout ? rgb(60, 32, 18) : rgb(10, 45, 110)).setStroke(); iris.lineWidth = 0.7; iris.stroke()
        (scout ? rgb(45, 22, 12, 0.85) : rgb(15, 40, 95, 0.8)).setFill(); oval(cx - 2, 28.5, 4, 4.4).fill()
        NSColor.white.setFill(); oval(cx - dir * 1.4 - 1.5, 30.6, 3, 3).fill()      // main highlight
        NSColor.white.withAlphaComponent(0.9).setFill(); oval(cx + dir * 1.6 - 0.7, 27.0, 1.4, 1.4).fill()
        NSGraphicsContext.restoreGraphicsState()
        let lash = NSBezierPath(); lash.lineWidth = 1.9; lash.lineCapStyle = .round; lash.lineJoinStyle = .round
        if levi {  // flat lid, lower at the inner corner: the unimpressed look
            lash.move(to: NSPoint(x: cx - dir * 5, y: 32)); lash.line(to: NSPoint(x: cx + dir * 5.6, y: 33.6))
            lash.lineWidth = 2.2; ink.setStroke(); lash.stroke()
            continue
        }
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

    } else if levi {
        // shaved sides showing under the cut, then a centre-parted fringe that falls in curtains to the eyes
        for x: CGFloat in [9.5, 50.5] {
            rgb(92, 88, 98).setFill(); NSBezierPath(roundedRect: NSRect(x: x, y: 27, width: 4, height: 12), xRadius: 2, yRadius: 2).fill()
        }
        let top = NSBezierPath(); top.move(to: NSPoint(x: 9, y: 41))
        top.curve(to: NSPoint(x: 32, y: 62), controlPoint1: NSPoint(x: 8, y: 56), controlPoint2: NSPoint(x: 18, y: 62))
        top.curve(to: NSPoint(x: 55, y: 41), controlPoint1: NSPoint(x: 46, y: 62), controlPoint2: NSPoint(x: 56, y: 56))
        for (x, y): (CGFloat, CGFloat) in [(52.5, 33), (48, 39.5), (44.5, 34.5), (40, 41), (34, 45), (30, 45), (24, 41),
                                           (19.5, 34.5), (16, 39.5), (11.5, 33)] {
            top.line(to: NSPoint(x: x, y: y))
        }
        top.close(); top.lineJoinStyle = .round
        hair.setFill(); top.fill(); hairDark.setStroke(); top.lineWidth = 0.9; top.stroke()
        for (x0, y0, x1, y1): (CGFloat, CGFloat, CGFloat, CGFloat) in [(32, 61, 32, 45.5), (28, 58, 20, 36), (36, 58, 44, 36),
                                                                       (24, 58, 13, 35), (40, 58, 51, 35)] {
            let st = NSBezierPath(); st.move(to: NSPoint(x: x0, y: y0)); st.line(to: NSPoint(x: x1, y: y1))
            NSColor.black.withAlphaComponent(0.5).setStroke(); st.lineWidth = 0.7; st.lineCapStyle = .round; st.stroke()
        }
        let shine = NSBezierPath(); shine.move(to: NSPoint(x: 19, y: 55.5))
        shine.curve(to: NSPoint(x: 28, y: 59.5), controlPoint1: NSPoint(x: 21.5, y: 58), controlPoint2: NSPoint(x: 25, y: 59.5))
        NSColor.white.withAlphaComponent(0.35).setStroke(); shine.lineWidth = 1.4; shine.lineCapStyle = .round; shine.stroke()
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

    if levi {  // a little laptop, screen out, a prompt waiting
        NSGraphicsContext.saveGraphicsState(); sh.set()
        let lid = NSBezierPath(roundedRect: NSRect(x: 15, y: 2, width: 34, height: 17), xRadius: 2.5, yRadius: 2.5)
        rgb(170, 175, 185).setFill(); lid.fill()
        NSGraphicsContext.restoreGraphicsState()
        rgb(30, 34, 42).setFill(); NSBezierPath(roundedRect: NSRect(x: 17, y: 4, width: 30, height: 13), xRadius: 1.5, yRadius: 1.5).fill()
        let p = NSAttributedString(string: ">_", attributes: [.font: NSFont.monospacedSystemFont(ofSize: 7, weight: .bold),
                                                              .foregroundColor: rgb(90, 230, 120)])
        p.draw(at: NSPoint(x: 19, y: 7.5))
        for x: CGFloat in [11.5, 45.5] {
            let hand = oval(x, 4, 7, 7); skin.setFill(); hand.fill(); skinEdge.setStroke(); hand.lineWidth = 0.9; hand.stroke()
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

// Busy animation over the pet, in drawPet's 64-unit space at time t (s): each pet shows its own kind of work.
func drawWork(_ b: NSRect, t: Double, scout: Bool = false, librarian: Bool = false, levi: Bool = false) {
    let k = min(b.width, b.height) / 64
    NSGraphicsContext.current?.cgContext.scaleBy(x: k, y: k)
    func rgb(_ r: CGFloat, _ g: CGFloat, _ bl: CGFloat, _ a: CGFloat = 1) -> NSColor {
        NSColor(srgbRed: r / 255, green: g / 255, blue: bl / 255, alpha: a)
    }
    func life(_ period: Double, _ offset: Double = 0) -> CGFloat { CGFloat((t / period + offset).truncatingRemainder(dividingBy: 1)) }
    func sparkle(_ x: CGFloat, _ y: CGFloat, _ r: CGFloat, _ c: NSColor) {  // four-point star
        let s = NSBezierPath(); s.move(to: NSPoint(x: x, y: y + r))
        for (dx, dy) in [(1, 0), (0, -1), (-1, 0), (0, 1)] as [(CGFloat, CGFloat)] {
            s.curve(to: NSPoint(x: x + dx * r, y: y + dy * r), controlPoint1: NSPoint(x: x, y: y), controlPoint2: NSPoint(x: x, y: y))
        }
        c.setFill(); s.fill()
    }

    if levi {  // code types out on the laptop a line at a time, cursor blinking; clears and starts over
        rgb(30, 34, 42).setFill(); NSBezierPath(roundedRect: NSRect(x: 17, y: 4, width: 30, height: 13), xRadius: 1.5, yRadius: 1.5).fill()
        let lines: [(indent: CGFloat, width: CGFloat, color: NSColor)] = [(0, 17, rgb(90, 230, 120)), (3, 12, rgb(120, 180, 255)), (3, 20, rgb(240, 200, 90))]
        var left = min(life(3) * 1.25, 1) * lines.reduce(0) { $0 + $1.width }, cursor = NSPoint(x: 19, y: 13)
        for (i, l) in lines.enumerated() where left > 0 {
            let y = 13 - CGFloat(i) * 3.4, n = min(left, l.width); left -= l.width
            l.color.setFill(); NSRect(x: 19 + l.indent, y: y, width: n, height: 1.6).fill()
            cursor = NSPoint(x: 19 + l.indent + n + 0.8, y: y)
        }
        if Int(t * 4) % 2 == 0 { rgb(90, 230, 120).setFill(); NSRect(x: cursor.x, y: cursor.y - 0.4, width: 1.5, height: 2.4).fill() }
        return
    }
    if scout {  // a radar sweep turns in the lens, sparkles pop around it: finds
        let c = NSPoint(x: 54.5, y: 16.5), a = CGFloat((t * 260).truncatingRemainder(dividingBy: 360))
        let wedge = NSBezierPath(); wedge.move(to: c); wedge.appendArc(withCenter: c, radius: 5.4, startAngle: a - 80, endAngle: a); wedge.close()
        rgb(60, 200, 255, 0.75).setFill(); wedge.fill()
        for i in 0..<3 {
            let p = life(1.1, Double(i) / 3), round = floor(t / 1.1 + Double(i) / 3)
            let ang = Double(i) * 2.1 + round * 1.7, r: CGFloat = 11
            let x = c.x + r * CGFloat(cos(ang)), y = max(c.y + r * CGFloat(sin(ang)), 4)
            sparkle(x, y, 4.2 * sin(p * .pi), rgb(255, 196, 30, sin(p * .pi)))
        }
        return
    }
    if librarian {  // pages turn right to left, a pause between each
        let p = min(life(1.4) / 0.65, 1), th = p * .pi, ex = 32 + 17.5 * cos(th), lift = 7 * sin(th)
        guard p < 1 else { return }
        let page = NSBezierPath(); page.move(to: NSPoint(x: 32, y: 3)); page.line(to: NSPoint(x: ex, y: 5.3 + lift))
        page.line(to: NSPoint(x: ex, y: 16.2 + lift)); page.line(to: NSPoint(x: 32, y: 14)); page.close()
        let shade = 0.82 + 0.18 * abs(cos(th))
        NSColor(white: shade, alpha: 1).blended(withFraction: 0.5, of: rgb(255, 250, 238))!.setFill(); page.fill()
        rgb(200, 185, 160).setStroke(); page.lineWidth = 0.6; page.stroke()
        return
    }
    // task pet: little letters rise out of the envelope, one to each side, and turn into green ticks
    for (i, side) in [(0, 1.0), (1, -1.0)] as [(Int, CGFloat)] {
        let p = life(1.8, Double(i) / 2), x = 32 + side * (17 + 13 * p), y = 13 + 26 * p - 12 * p * p  // up past the cheeks, clear of the face
        if p < 0.6 {
            let a = min(1, p * 6), r = NSRect(x: x - 3.5, y: y - 2.2, width: 7, height: 4.6)
            rgb(255, 250, 240, a).setFill(); NSBezierPath(roundedRect: r, xRadius: 0.8, yRadius: 0.8).fill()
            let v = NSBezierPath(); v.move(to: NSPoint(x: r.minX + 0.6, y: r.maxY - 0.6)); v.line(to: NSPoint(x: r.midX, y: r.midY - 0.3))
            v.line(to: NSPoint(x: r.maxX - 0.6, y: r.maxY - 0.6))
            rgb(40, 96, 190, a).setStroke(); v.lineWidth = 0.8; v.stroke(); NSBezierPath(roundedRect: r, xRadius: 0.8, yRadius: 0.8).stroke()
        } else {
            let a = 1 - (p - 0.6) / 0.4, tick = NSBezierPath()
            tick.move(to: NSPoint(x: x - 2.6, y: y)); tick.line(to: NSPoint(x: x - 0.6, y: y - 2)); tick.line(to: NSPoint(x: x + 3, y: y + 2.4))
            rgb(52, 180, 90, a).setStroke(); tick.lineWidth = 1.8; tick.lineCapStyle = .round; tick.lineJoinStyle = .round; tick.stroke()
        }
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
    var onMoved: () -> Void = {}
    var makeMenu: () -> NSMenu = { NSMenu() }
    var scout = false, librarian = false, levi = false, frameName = "pet", badgeColor = NSColor.systemRed
    var grab = NSPoint.zero, moved = false
    var overdue = 0 { didSet { if overdue != oldValue { needsDisplay = true } } }  // badge count
    var agents = 0 { didSet { if agents != oldValue { needsDisplay = true } } }    // Claude Code / Codex sessions working
    var agentsWaiting = false { didSet { if agentsWaiting != oldValue { needsDisplay = true } } }  // one needs you
    var hover = false { didSet { if hover != oldValue { needsDisplay = true } } }  // something dragged over it
    var busy = false { didSet { if busy != oldValue { animate() } } }
    var tuckedInto: NSRectEdge? { didSet { if tuckedInto != oldValue { needsDisplay = true } } }  // the screen edge it's tucked into
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
    override func draw(_ r: NSRect) {
        // Fully transparent pixels let clicks fall through; a near-invisible fill makes the whole square clickable.
        NSColor.black.withAlphaComponent(0.01).setFill(); bounds.fill()
        if let e = tuckedInto { return drawTab(e) }
        // pose: lean in while something is dragged over, hop / shake once, sway while busy
        let now = Date().timeIntervalSinceReferenceDate, t = Date().timeIntervalSince(animStart)
        var dx: CGFloat = 0, dy: CGFloat = 0, turn: CGFloat = 0, grow: CGFloat = hover ? 1.08 : 1
        if let a = anim {
            if t > 0.5 { anim = nil }
            else if a == .hop { dy = 9 * sin(t / 0.5 * .pi) }
            else { dx = 5 * sin(t * 45) * (1 - t / 0.5) }
        }
        if busy {  // each pet works its own way: Levi types, Scout scans, the Librarian reads, the Task pet bobs along
            if levi { dy = 0.8 * abs(sin(now * 16)) }
            else if scout { dx += 3 * sin(now * 2); turn = 3 * sin(now * 2) }
            else if librarian { turn = 2.5 * sin(now * 1.6) }
            else { dy += 1.5 * abs(sin(now * 4)) }
        }
        let pet = NSRect(x: (bounds.width - 96) / 2, y: 3, width: 96, height: 96)
        NSGraphicsContext.saveGraphicsState()
        let cg = NSGraphicsContext.current!.cgContext
        cg.translateBy(x: pet.midX + dx, y: pet.minY + dy)  // pivot at the feet
        cg.rotate(by: turn * .pi / 180); cg.scaleBy(x: grow, y: grow)
        cg.translateBy(x: -48, y: 0)
        cg.saveGState(); drawPet(NSRect(x: 0, y: 0, width: 96, height: 96), scout: scout, librarian: librarian, levi: levi); cg.restoreGState()
        if busy { drawWork(NSRect(x: 0, y: 0, width: 96, height: 96), t: now, scout: scout, librarian: librarian, levi: levi) }
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
        if agents > 0 {  // bottom-left: green while agents work, purple when one is waiting for you
            let s = NSAttributedString(string: "▸\(agents > 9 ? "9+" : "\(agents)")", attributes: [
                .font: NSFont.boldSystemFont(ofSize: 11), .foregroundColor: NSColor.white])
            let r = NSRect(x: 2, y: 2, width: max(28, s.size().width + 12), height: 22)
            let pill = NSBezierPath(roundedRect: r, xRadius: 11, yRadius: 11)
            (agentsWaiting ? NSColor.systemPurple : NSColor.systemGreen).setFill(); pill.fill()
            NSColor.white.setStroke(); pill.lineWidth = 2; pill.stroke()
            s.draw(at: NSPoint(x: r.midX - s.size().width / 2, y: r.midY - s.size().height / 2))
        }
        guard overdue > 0 else { return }
        let text = NSAttributedString(string: overdue > 99 ? "99+" : "\(overdue)", attributes: [
            .font: NSFont.boldSystemFont(ofSize: 15), .foregroundColor: NSColor.white])
        let w = max(26, text.size().width + 12), r = NSRect(x: bounds.maxX - w - 2, y: bounds.maxY - 28, width: w, height: 26)
        let pill = NSBezierPath(roundedRect: r, xRadius: 13, yRadius: 13)
        badgeColor.setFill(); pill.fill(); NSColor.white.setStroke(); pill.lineWidth = 2; pill.stroke()
        text.draw(at: NSPoint(x: r.midX - text.size().width / 2, y: r.midY - text.size().height / 2))
    }
    var tabLong: CGFloat { overdue > 0 || agents > 0 ? 72 : 40 }  // a tab's length along the edge: face, then count
    func drawTab(_ e: NSRectEdge) {  // tucked: a tab in the sliver left showing, the pet's face and its count
        let n = overdue > 0 ? overdue : agents, side = e == .minX || e == .maxX, long = tabLong
        let a = e == .minX ? bounds.maxX - sliver : e == .minY ? bounds.maxY - sliver : 0  // where the on-screen strip starts
        var tab = side ? NSRect(x: a + 1, y: bounds.midY - long / 2, width: sliver - 2, height: long)
                       : NSRect(x: bounds.midX - long / 2, y: a + 1, width: long, height: sliver - 2)
        switch e {  // run the tab off the screen edge, so only its inner corners are round
        case .minX: tab.origin.x -= 12; tab.size.width += 12
        case .maxX: tab.size.width += 12
        case .minY: tab.origin.y -= 12; tab.size.height += 12
        default: tab.size.height += 12
        }
        let shape = NSBezierPath(roundedRect: tab, xRadius: 14, yRadius: 14)
        NSGraphicsContext.saveGraphicsState()
        let sh = NSShadow(); sh.shadowBlurRadius = 3; sh.shadowOffset = NSSize(width: 0, height: -1)
        sh.shadowColor = NSColor.black.withAlphaComponent(0.3); sh.set()
        NSColor(white: 0.98, alpha: 1).setFill(); shape.fill()
        NSGraphicsContext.restoreGraphicsState()
        NSColor(white: 0, alpha: 0.15).setStroke(); shape.lineWidth = 1; shape.stroke()
        // the face first (top of a side tab, left of a bottom/top one), the count after it
        let c = side ? NSPoint(x: a + sliver / 2, y: bounds.midY + long / 2 - 20) : NSPoint(x: bounds.midX - long / 2 + 20, y: a + sliver / 2)
        NSGraphicsContext.saveGraphicsState()
        NSBezierPath(ovalIn: NSRect(x: c.x - 16, y: c.y - 16, width: 32, height: 32)).addClip()
        let cg = NSGraphicsContext.current!.cgContext, k: CGFloat = 0.67  // the 64-unit pet at this scale: its head fills the circle
        cg.translateBy(x: c.x - 32 * k, y: c.y - 43 * k)
        drawPet(NSRect(x: 0, y: 0, width: 64 * k, height: 64 * k), scout: scout, librarian: librarian, levi: levi)
        NSGraphicsContext.restoreGraphicsState()
        guard n > 0 else { return }
        let text = NSAttributedString(string: n > 99 ? "99+" : n > 9 && overdue == 0 ? "9+" : "\(n)", attributes: [
            .font: NSFont.boldSystemFont(ofSize: n > 99 ? 10 : n > 9 ? 12 : 14), .foregroundColor: NSColor.white])
        let d = side ? NSPoint(x: c.x, y: c.y - 33) : NSPoint(x: c.x + 33, y: c.y)
        let dot = NSBezierPath(ovalIn: NSRect(x: d.x - 13, y: d.y - 13, width: 26, height: 26))
        (overdue > 0 ? badgeColor : agentsWaiting ? NSColor.systemPurple : NSColor.systemGreen).setFill(); dot.fill()
        text.draw(at: NSPoint(x: d.x - text.size().width / 2, y: d.y - text.size().height / 2))
    }
    override func acceptsFirstMouse(for e: NSEvent?) -> Bool { true }
    override func rightMouseDown(with e: NSEvent) { NSMenu.popUpContextMenu(makeMenu(), with: e, for: self) }
    override func mouseDown(with e: NSEvent) {
        grab = e.locationInWindow; moved = false
        if e.modifierFlags.contains(.control) { moved = true; rightMouseDown(with: e) }  // ctrl-click = right-click
    }
    override func mouseDragged(with e: NSEvent) {
        let p = NSEvent.mouseLocation, at = convert(e.locationInWindow, from: nil), from = convert(grab, from: nil)
        if !moved && hypot(at.x - from.x, at.y - from.y) < 4 { return }  // a shaky click is still a click
        moved = true
        window?.setFrameOrigin(NSPoint(x: p.x - grab.x, y: p.y - grab.y))
    }
    override func mouseUp(with e: NSEvent) {
        if moved { keepOnScreen(); onMoved() } else { onClick() }
    }
    func keepOnScreen() {  // all of the pet stays clickable: pull it back inside the screen, snap to an edge within 24 pt
        if let w = window { w.setFrameOrigin(onScreen(w.frame)) }
    }
    static let dragTypes = DropWebView.promiseTypes + [.fileURL, .URL, .string]
    override func draggingEntered(_ s: NSDraggingInfo) -> NSDragOperation { hover = true; return .copy }
    override func draggingExited(_ s: NSDraggingInfo?) { hover = false }
    override func performDragOperation(_ s: NSDraggingInfo) -> Bool { hover = false; return onDrop(s.draggingPasteboard) }
}

func screenOf(_ r: NSRect) -> NSScreen {  // the screen a pet's rect is centered on (or the main one, if off every screen)
    NSScreen.screens.first { $0.frame.contains(NSPoint(x: r.midX, y: r.midY)) } ?? NSScreen.main!
}
// Pet at r tucked into an edge of its screen (frame f, visible v), a sliver left showing: its nearer side, else
// the other side, else the bottom, else the top. An edge another screen touches (others) is skipped: the pet would
// show up on that screen instead. Stays at r if every edge is shared.
func tuckSpot(_ r: NSRect, _ f: NSRect, _ v: NSRect, _ others: [NSRect]) -> NSRect {
    let w = r.width, h = r.height, sides = [  // (tucked rect, a 1 pt strip just past that edge)
        (NSRect(x: v.minX - w + sliver, y: r.minY, width: w, height: h), NSRect(x: f.minX - 1, y: r.minY, width: 1, height: h)),
        (NSRect(x: v.maxX - sliver, y: r.minY, width: w, height: h), NSRect(x: f.maxX, y: r.minY, width: 1, height: h)),
        (NSRect(x: r.minX, y: v.minY - h + sliver, width: w, height: h), NSRect(x: r.minX, y: f.minY - 1, width: w, height: 1)),
        (NSRect(x: r.minX, y: v.maxY - sliver, width: w, height: h), NSRect(x: r.minX, y: f.maxY, width: w, height: 1))]
    let order = r.midX < v.midX ? [0, 1, 2, 3] : [1, 0, 2, 3]
    return order.map { sides[$0] }.first { side in !others.contains { $0.intersects(side.1) } }?.0 ?? r
}
func onScreen(_ r: NSRect) -> NSPoint {  // r pulled back inside its screen, snapped to an edge within 24 pt
    let v = screenOf(r).visibleFrame
    func fit(_ a: CGFloat, _ lo: CGFloat, _ hi: CGFloat) -> CGFloat { a - lo < 24 ? lo : hi - a < 24 ? hi : a }
    return NSPoint(x: fit(r.minX, v.minX, v.maxX - r.width), y: fit(r.minY, v.minY, v.maxY - r.height))
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
    var window: NSWindow!, feedWindow: NSWindow!, libWindow: NSWindow!, settingsWindow: NSWindow!, leviWindow: NSWindow!
    var pets: [String: (panel: NSPanel, view: PetView, window: NSWindow?)] = [:]  // "task", "scout", "library", "levi"
    var bubbles: [String: Bubble] = [:], lastBubble = 0
    var routines: [[String: String]] = [], agents: [[String: Any]] = []
    var feedWeb: WKWebView!, libWeb: DropWebView!, settingsWeb: WKWebView!, leviWeb: WKWebView!
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
        // the Dock caches app icons; setting it here shows a new icon right away
        if let name = Bundle.main.object(forInfoDictionaryKey: "CFBundleIconFile") as? String { NSApp.applicationIconImage = NSImage(named: name) }
        let logPath = NSHomeDirectory() + "/Library/Logs/SurveyCorps.log"
        FileManager.default.createFile(atPath: logPath, contents: nil)
        let log = FileHandle(forWritingAtPath: logPath)
        server.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        server.arguments = [appPy, "--no-browser"]
        server.standardOutput = log
        server.standardError = log
        try? server.run()  // if a server is already running, this one exits and we use that one

        let mainConfig = WKWebViewConfiguration()
        mainConfig.userContentController.add(self, name: "shot")  // the page's camera button
        let web = DropWebView(frame: .zero, configuration: mainConfig)
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

        // Levi window: starts hidden, shown by Levi; session links (claude://, codex://, vscode://) open via createWebViewWith
        leviWeb = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        leviWeb.navigationDelegate = self
        leviWeb.uiDelegate = self
        leviWindow = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 640),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        leviWindow.title = "Coding Sessions"
        leviWindow.minSize = NSSize(width: 520, height: 320)
        leviWindow.contentView = leviWeb
        leviWindow.center()
        leviWindow.setFrameAutosaveName("levi")
        leviWindow.isReleasedWhenClosed = false
        leviWeb.load(URLRequest(url: leviURL))

        let all: [(String, NSWindow?, String)] = [("task", window, "Task Manager"), ("scout", feedWindow, "Scout: new papers, videos and repos"),
                                                  ("library", libWindow, "Library: your projects, files and docs"),
                                                  ("levi", leviWindow, "Levi: your Claude Code and Codex sessions")]
        for (i, (kind, w, tip)) in all.enumerated() {
            let v = PetView()
            v.scout = kind == "scout"; v.librarian = kind == "library"; v.levi = kind == "levi"
            v.frameName = ["task": "pet", "scout": "scoutPet", "library": "libPet", "levi": "leviPet"][kind]!
            if kind == "scout" { v.badgeColor = .systemBlue }
            if let w {
                v.toolTip = tip + (kind == "levi" ? ". Click to show/hide, drag to move, right-click for more"
                                                  : ". Click to show/hide, drag to move, drop things on it, right-click for more")
                v.onClick = { [unowned self] in self.toggle(w) }
                if kind != "levi" { v.onDrop = { [unowned self] in self.drop(kind, $0) } }
            }
            v.makeMenu = { [unowned self] in self.menu(kind) }
            v.onMoved = { [unowned self, unowned v] in
                self.setHome(kind, v.window!.frame.origin)
                if self.pref("petsLinedUp") { self.lineUp(near: kind) }
            }
            let panel = makePet(v, offset: 26 + CGFloat(i) * (petSize + 4))
            pets[kind] = (panel, v, w)
            let saved = UserDefaults.standard.string(forKey: "petHome." + kind).map(NSPointFromString) ?? panel.frame.origin
            setHome(kind, onScreen(NSRect(origin: saved, size: panel.frame.size)))
        }
        if pref("petsLinedUp") { lineUp() }
        if pref("petsHidden") { setPetsHidden(true) }
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [unowned self] _ in self.tick() }
        NotificationCenter.default.addObserver(self, selector: #selector(screensChanged),
                                               name: NSApplication.didChangeScreenParametersNotification, object: nil)
        Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [unowned self] _ in self.poll() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.poll() }
    }

    // Everything the pets show comes from one call: badges, who's busy, new speech bubbles.
    func poll() {
        URLSession.shared.dataTask(with: URL(string: "\(base)/api/pets?after=\(lastBubble)")!) { data, _, _ in
            let s = json(data)
            guard !s.isEmpty else { return }
            DispatchQueue.main.async {
                let busy = s["busy"] as? [String: Bool] ?? [:]
                self.routines = s["routines"] as? [[String: String]] ?? []
                self.agents = s["agents"] as? [[String: Any]] ?? []
                let active = self.agents.filter { $0["state"] as? String != "idle" }
                self.pets["levi"]?.view.agents = active.count
                self.pets["levi"]?.view.agentsWaiting = active.contains { $0["state"] as? String == "waiting" }
                for (k, p) in self.pets { p.view.busy = busy[k] ?? false }
                self.pets["task"]?.view.overdue = s["overdue"] as? Int ?? 0  // red: open tasks past their due time
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
        if pref("petsHidden") { setPetsHidden(false) }  // hidden pets come back when they have something to say
        tucked = false; awaySince = Date(); settle(animate: false)  // out at home before the bubble is placed over it
        let b = bubbles[kind] ?? Bubble()
        bubbles[kind] = b
        b.onClick = { NSApp.activate(ignoringOtherApps: true); p.window?.makeKeyAndOrderFront(nil) }
        b.show(title, text, over: p.panel)
        p.view.play(ok ? .hop : .shake)
        if !pref("petsMute") {  // each pet has its own voice; a low thud when something failed
            NSSound(named: ok ? ["task": "Glass", "scout": "Pop", "library": "Bottle", "levi": "blade"][kind] ?? "Tink" : "Funk")?.play()
        }
    }

    // Drop onto a pet: emails/text -> a task, a link -> Scout and the Library, files/folders -> the Library.
    func drop(_ kind: String, _ pb: NSPasteboard) -> Bool {
        let files = pb.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
        let text = pb.string(forType: .string)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        switch kind {
        case "library":
            let link = (pb.readObjects(forClasses: [NSURL.self]) as? [URL])?.first { !$0.isFileURL }?.absoluteString ?? text
            guard !files.isEmpty || link.hasPrefix("http") else { say(kind, "", "Drop files, folders or a link on me", ok: false); return false }
            addToLibrary(files.isEmpty ? [link] : files.map(\.path))
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

    // Hide the app, let the user drag over an area with macOS's own picker (Space: a window, Esc: cancel),
    // then Argo reads the image into a task. First use asks for Screen Recording permission.
    func screenshotTask() {
        let shown = window.isVisible
        NSApp.hide(nil)
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("survey-corps-shot.png")
        try? FileManager.default.removeItem(at: file)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {  // let the window finish hiding first
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
            p.arguments = ["-i", "-x", file.path]
            p.terminationHandler = { _ in DispatchQueue.main.async {
                if shown { NSApp.activate(ignoringOtherApps: true); self.window.makeKeyAndOrderFront(nil) }
                guard FileManager.default.fileExists(atPath: file.path) else { return }  // Esc: nothing taken
                uploadEmail(file) { title in
                    self.say("task", "", title != nil ? "Added the screenshot. Argo is reading it." : "Couldn't add the screenshot", ok: title != nil)
                }
            } }
            try? p.run()
        }
    }

    func addTask(_ text: String) {
        post("api/drop", raw: Data(text.utf8)) { d, code in
            self.say("task", "", code == 200 ? "Added: \(json(d)["title"] as? String ?? text)" : "Couldn't add that task", ok: code == 200)
        }
    }

    func saveLink(_ link: String) {
        post("api/feed/add", ["url": link]) { d, code in
            let r = json(d), title = r["title"] as? String ?? link
            self.say("scout", "", code == 200 ? (r["existing"] as? Bool == true ? "Already in the Library: " : "Added to the Library: ") + title
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

    func menu(_ kind: String) -> NSMenu {
        let m = NSMenu(), p = pets[kind]!
        m.autoenablesItems = false
        if let w = p.window {
            m.addItem(Item((w.isVisible ? "Hide " : "Show ") + w.title) { self.toggle(w) })
            m.addItem(.separator())
        }
        switch kind {
        case "task":
            m.addItem(Item("Quick Add Task…") {
                if let v = textPrompt("New task", info: "With Argo, plain language works.", placeholder: "review Bob's draft by Fri 3pm") { self.addTask(v) }
            })
            m.addItem(Item("Screenshot to Task…") { self.screenshotTask() })
            m.addItem(Item("Ask About My Tasks…") { self.ask(kind, "api/ask", "Ask about your tasks and emails", "what did Bob ask me about the beamtime?") })
        case "levi": break  // just show/hide; the window has the sessions
        case "scout":
            m.addItem(Item("Check for New Now") { post("api/feed/refresh") { _, _ in self.poll() } })
            m.addItem(Item("Save a Link…") { if let v = textPrompt("Add a link to Scout and the Library", placeholder: "https://arxiv.org/abs/…") { self.saveLink(v) } })
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
        if kind != "levi" { m.addItem(.separator()) }
        let mine = routines.filter { $0["pet"] == kind }
        if !mine.isEmpty {
            let run = NSMenuItem(title: "Run Routine Now", action: nil, keyEquivalent: ""), sub = NSMenu()
            for r in mine { sub.addItem(Item(r["line"] ?? "") { post("api/routines/run", ["line": r["line"] ?? ""]) { _, _ in self.poll() } }) }
            run.submenu = sub
            m.addItem(run)
        }
        if kind != "levi" { m.addItem(Item("Edit Routines…") { self.openSettings() }); m.addItem(.separator()) }
        let all = NSMenuItem(title: "All Pets", action: nil, keyEquivalent: ""), sub = NSMenu()
        sub.autoenablesItems = false
        sub.addItem(Item("Line Up Here") { self.lineUp(near: kind) })
        let sound = Item("Sounds") { self.flip("petsMute") }
        sound.state = pref("petsMute") ? .off : .on
        sub.addItem(sound)
        for (title, key) in [("Keep Lined Up", "petsLinedUp"), ("Tuck Into the Edge When the Mouse Is Away", "petsTuck")] {
            let i = Item(title) {
                self.flip(key)
                if key == "petsLinedUp" && self.pref(key) { self.lineUp(near: kind) }
            }
            i.state = pref(key) ? .on : .off
            sub.addItem(i)
        }
        sub.addItem(Item("Hide Pets (⇧⌘P or the Dock icon brings them back)") { self.setPetsHidden(true) })
        all.submenu = sub
        m.addItem(all)
        return m
    }

    // Pet layout: each pet has a home spot (where you dropped it, or where Line Up put it). Every tick moves each pet
    // to where it belongs right now, home or tucked, rather than remembering what it did last: a slide or fade cut short
    // (by a bubble, a display change, a drag) gets finished on the next tick instead of leaving a pet stuck.
    var tucked = false, home: [String: NSPoint] = [:], awaySince = Date(), settleUntil = Date.distantPast
    func pref(_ k: String) -> Bool { UserDefaults.standard.bool(forKey: k) }
    func flip(_ k: String) { UserDefaults.standard.set(!pref(k), forKey: k) }
    func setHome(_ kind: String, _ o: NSPoint) {
        home[kind] = o
        UserDefaults.standard.set(NSStringFromPoint(o), forKey: "petHome." + kind)
    }
    func homeRect(_ kind: String) -> NSRect { NSRect(origin: home[kind] ?? .zero, size: NSSize(width: petSize, height: petSize)) }
    func lineUp(near kind: String = "task") {  // a column on the side and half of the screen that pet's home is on
        let a = homeRect(kind), v = screenOf(a).visibleFrame
        let left = a.midX < v.midX, top = a.midY > v.midY
        for (i, k) in ["task", "scout", "library", "levi"].enumerated() where pets[k] != nil {
            setHome(k, NSPoint(x: left ? v.minX : v.maxX - petSize, y: top ? v.maxY - CGFloat(i + 1) * (petSize + 4) : v.minY + CGFloat(i) * (petSize + 4)))
        }
        tucked = false; awaySince = Date(); settle()
    }
    func spots() -> [String: NSRect] {  // where each pet belongs now; tabs tucked into one edge close up into a column
        var out = [String: NSRect]()
        for k in pets.keys {
            let r = homeRect(k), s = screenOf(r)
            out[k] = tucked ? tuckSpot(r, s.frame, s.visibleFrame, NSScreen.screens.filter { $0 != s }.map(\.frame)) : r
        }
        let moved = out.keys.filter { out[$0] != homeRect($0) }
        for (_, ks) in Dictionary(grouping: moved, by: { out[$0]!.minX != homeRect($0).minX ? "x\(out[$0]!.minX)" : "y\(out[$0]!.minY)" }) {
            let side = out[ks[0]]!.minX != homeRect(ks[0]).minX  // a side edge: a column, top first; else a row, left first
            let along = { (k: String) in side ? -out[k]!.midY : out[k]!.midX }
            let row = ks.sorted { along($0) < along($1) }, len = { (k: String) in self.pets[k]!.view.tabLong }
            var at = row.map(along).reduce(0, +) / CGFloat(row.count) - (row.map(len).reduce(0, +) + CGFloat(row.count - 1) * 6) / 2
            for k in row {  // centered on where the pets were, 6 pt apart
                let mid = at + len(k) / 2
                if side { out[k]!.origin.y = -mid - petSize / 2 } else { out[k]!.origin.x = mid - petSize / 2 }
                at += len(k) + 6
            }
        }
        return out
    }
    func tick() {  // tucking: out while the mouse is within 50 pt or a bubble shows, back in after 3 s away
        let m = NSEvent.mouseLocation
        if !pref("petsTuck") || pref("petsHidden") || bubbles.values.contains(where: \.isVisible)
            || pets.values.contains(where: { $0.panel.frame.insetBy(dx: -50, dy: -50).contains(m) }) {
            awaySince = Date(); tucked = false
        } else if Date().timeIntervalSince(awaySince) > 3 { tucked = true }
        if NSEvent.pressedMouseButtons == 0 && Date() > settleUntil { settle() }  // not mid-drag, not mid-slide
    }
    func settle(animate: Bool = true) {  // move any pet that isn't where spot() says
        let to = spots()
        for (k, p) in pets {  // tucked pets draw as a tab, on the side that's still on screen
            let t = to[k]!, h = homeRect(k), v = screenOf(h).visibleFrame
            p.view.tuckedInto = t == h ? nil : t.maxX <= v.minX + sliver + 1 ? .minX : t.minX >= v.maxX - sliver - 1 ? .maxX
                                : t.maxY <= v.minY + sliver + 1 ? .minY : .maxY
        }
        let off = pets.keys.filter { k in
            let f = to[k]!, p = pets[k]!.panel
            return hypot(f.minX - p.frame.minX, f.minY - p.frame.minY) > 0.5 || p.alphaValue < 1
        }
        guard !off.isEmpty else { return }
        settleUntil = Date() + 0.35
        NSAnimationContext.runAnimationGroup { c in
            c.duration = animate ? 0.25 : 0
            for k in off {
                let f = to[k]!, p = pets[k]!.panel
                if animate { p.animator().setFrame(f, display: true); p.animator().alphaValue = 1 }
                else { p.setFrame(f, display: true); p.alphaValue = 1 }
            }
        }
    }
    @objc func screensChanged() {  // a display plugged in, unplugged or rearranged, or the Dock moved
        for k in pets.keys { setHome(k, onScreen(homeRect(k))) }
        if pref("petsLinedUp") { lineUp() }
        settle(animate: false)
    }
    func setPetsHidden(_ h: Bool) {
        UserDefaults.standard.set(h, forKey: "petsHidden")
        for p in pets.values { h ? p.panel.orderOut(nil) : p.panel.orderFrontRegardless() }
        if h { bubbles.values.forEach { $0.orderOut(nil) } }
    }

    func makePet(_ view: PetView, offset: CGFloat) -> NSPanel {
        let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: petSize, height: petSize),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.level = .floating
        p.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
        p.backgroundColor = .clear
        p.isOpaque = false
        p.hasShadow = false
        p.ignoresMouseEvents = false  // set explicitly, so clear pixels take clicks too: the whole square is the pet
        p.contentView = view
        view.registerForDraggedTypes(PetView.dragTypes)
        if !p.setFrameUsingName(view.frameName), let f = NSScreen.main?.visibleFrame {
            p.setFrameOrigin(NSPoint(x: f.maxX - petSize - offset, y: f.minY + 20))  // bottom-right corner
        }
        p.setContentSize(NSSize(width: petSize, height: petSize))  // saved frame may hold an old size
        view.keepOnScreen()
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
        if m.name == "shot" { return screenshotTask() }
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
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { w.load(URLRequest(url: w === self.feedWeb ? feedURL : w === self.libWeb ? libURL : w === self.settingsWeb ? settingsURL : w === self.leviWeb ? leviURL : base)) }
    }

    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if pref("petsHidden") { setPetsHidden(false) }
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
let petsItem = Item("Show or Hide Pets") { delegate.setPetsHidden(!delegate.pref("petsHidden")) }
petsItem.keyEquivalent = "p"; petsItem.keyEquivalentModifierMask = [.command, .shift]
appMenu.addItem(petsItem)
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
