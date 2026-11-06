// Tito Repricer - native macOS window for the repricing cockpit.
// Built on the user's Mac by install.sh (swiftc from Command Line Tools).
import Cocoa
import WebKit

let BASE = "http://localhost:8766/"

class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var web: WKWebView!

    func applicationDidFinishLaunching(_ notification: Notification) {
        startServer()

        let config = WKWebViewConfiguration()
        let flag = WKUserScript(source: "window.__nativeApp = true;", injectionTime: .atDocumentStart, forMainFrameOnly: true)
        config.userContentController.addUserScript(flag)
        config.userContentController.add(self, name: "app")
        web = WKWebView(frame: .zero, configuration: config)
        web.uiDelegate = self
        web.navigationDelegate = self

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1320, height: 920),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Tito Repricer"
        window.isReleasedWhenClosed = false
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("TitoRepricerMain")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        web.loadHTMLString("<body style='font:600 17px -apple-system;color:#6b7078;display:flex;align-items:center;justify-content:center;height:90vh'>Starting Tito Repricer…</body>", baseURL: nil)
        waitForServer(attempt: 0)
    }

    func startServer() {
        guard let script = Bundle.main.path(forResource: "start", ofType: "sh") else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [script]
        try? p.run()
    }

    func waitForServer(attempt: Int) {
        var req = URLRequest(url: URL(string: BASE + "api/state")!)
        req.timeoutInterval = 1
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            DispatchQueue.main.async {
                if let r = resp as? HTTPURLResponse, r.statusCode == 200 {
                    self.web.load(URLRequest(url: URL(string: BASE)!))
                } else if attempt < 80 {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.waitForServer(attempt: attempt + 1) }
                } else {
                    self.web.loadHTMLString("<body style='font:600 17px -apple-system;padding:40px;color:#b5462c'>Tito Repricer did not start. Close this window and open it again.</body>", baseURL: nil)
                }
            }
        }.resume()
    }

    // Red button only closes the window; the app stays in the Dock (like other Mac apps)
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { return false }

    // Clicking the Dock icon brings the window back
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { showWindow() }
        return true
    }

    func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        web.evaluateJavaScript("try{(document.getElementById('scan')).focus()}catch(e){}", completionHandler: nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        var req = URLRequest(url: URL(string: BASE + "api/quit")!)
        req.httpMethod = "POST"
        req.timeoutInterval = 1
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { _, _, _ in done.signal() }.resume()
        _ = done.wait(timeout: .now() + 1.5)
    }

    // Messages from the page: "quit"
    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage) {
        if let s = message.body as? String, s == "quit" { NSApp.terminate(nil) }
    }

    // Links that open a new window (e.g. "Apply to sell") go to the default browser
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = navigationAction.request.url { NSWorkspace.shared.open(u) }
        return nil
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)

// Menus: Quit + Edit (so Cmd+C / Cmd+V / Cmd+A work in text boxes)
let mainMenu = NSMenu()
let appItem = NSMenuItem()
mainMenu.addItem(appItem)
let appMenu = NSMenu()
appMenu.addItem(withTitle: "Hide Tito Repricer", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
appMenu.addItem(NSMenuItem.separator())
appMenu.addItem(withTitle: "Quit Tito Repricer", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
appItem.submenu = appMenu
let editItem = NSMenuItem()
mainMenu.addItem(editItem)
let editMenu = NSMenu(title: "Edit")
editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
editMenu.addItem(NSMenuItem.separator())
editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
editItem.submenu = editMenu
let winItem = NSMenuItem()
mainMenu.addItem(winItem)
let winMenu = NSMenu(title: "Window")
winMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
winMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
winItem.submenu = winMenu
app.mainMenu = mainMenu
app.windowsMenu = winMenu

app.run()
