// YT Grab — native macOS shell.
// Starts the bundled Python server (Resources/ytgrab/app.py) and shows its UI in a WKWebView window.
// Build with build-app.sh; requires Xcode Command Line Tools (swiftc) and Homebrew's python3/ffmpeg/yt-dlp.

import Cocoa
import WebKit

let appBackground = NSColor(srgbRed: 0x13 / 255.0, green: 0x15 / 255.0, blue: 0x19 / 255.0, alpha: 1)

/// Invisible strip over the top of the window: lets the user drag/zoom the window
/// even though the web view fills the area under the transparent title bar.
final class DragStrip: NSView {
    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 {
            window?.performZoom(nil)
        } else {
            window?.performDrag(with: event)
        }
    }
}

/// Web view that hands Finder drops to the page as real file paths
/// (a plain browser only exposes file names).
final class AppWebView: WKWebView {
    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        let options: [NSPasteboard.ReadingOptionKey: Any] = [.urlReadingFileURLsOnly: true]
        if let urls = sender.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: options) as? [URL],
           !urls.isEmpty {
            let paths = urls.map { $0.path }
            if let data = try? JSONSerialization.data(withJSONObject: paths),
               let json = String(data: data, encoding: .utf8) {
                evaluateJavaScript("window.ytgrabNative && window.ytgrabNative.drop(\(json))", completionHandler: nil)
            }
            return true
        }
        return super.performDragOperation(sender)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    var window: NSWindow!
    var webView: AppWebView!
    var server: Process?
    var serverOutput = ""
    var serverURL: URL?
    var logHandle: FileHandle?
    var quitting = false

    // MARK: lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        startServer()
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard serverURL != nil, webView != nil else { return .terminateNow }
        // `state` is a top-level `let` in the page, so it is not a property of `window`.
        let script = "(typeof state !== 'undefined' && state.jobs ? state.jobs : []).filter(function (j) { return j.status === 'running' || j.status === 'queued'; }).length"
        webView.evaluateJavaScript(script) { result, _ in
            let active = (result as? NSNumber)?.intValue ?? 0
            if active == 0 {
                sender.reply(toApplicationShouldTerminate: true)
                return
            }
            let alert = NSAlert()
            alert.messageText = "Ще йдуть завдання: \(active)"
            alert.informativeText = "Якщо вийти зараз, завантаження й конвертація зупиняться."
            alert.addButton(withTitle: "Вийти")
            alert.addButton(withTitle: "Скасувати")
            sender.reply(toApplicationShouldTerminate: alert.runModal() == .alertFirstButtonReturn)
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        stopServer()
    }

    // MARK: window

    func buildWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1180, height: 780),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "YT Grab"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.backgroundColor = appBackground
        window.minSize = NSSize(width: 900, height: 600)
        window.appearance = NSAppearance(named: .darkAqua)
        window.isReleasedWhenClosed = false
        if !window.setFrameUsingName("YTGrabMain") {
            window.center()
        }
        window.setFrameAutosaveName("YTGrabMain")

        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "ytgrab")
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        webView = AppWebView(frame: .zero, configuration: config)
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")
        webView.translatesAutoresizingMaskIntoConstraints = false

        let strip = DragStrip()
        strip.translatesAutoresizingMaskIntoConstraints = false

        let content = window.contentView!
        content.addSubview(webView)
        content.addSubview(strip)
        NSLayoutConstraint.activate([
            webView.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            webView.topAnchor.constraint(equalTo: content.topAnchor),
            webView.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            strip.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 80),
            strip.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            strip.topAnchor.constraint(equalTo: content.topAnchor),
            strip.heightAnchor.constraint(equalToConstant: 34),
        ])

        showMessage("Запускаю YT Grab…", detail: "")
        window.makeKeyAndOrderFront(nil)
    }

    func showMessage(_ title: String, detail: String) {
        let safeTitle = title.replacingOccurrences(of: "<", with: "&lt;")
        let safeDetail = detail.replacingOccurrences(of: "<", with: "&lt;")
        let html = """
        <html><body style="margin:0;height:100vh;display:grid;place-items:center;background:#131519;color:#eceef2;
        font:14px -apple-system,sans-serif;-webkit-user-select:none">
        <div style="text-align:center;max-width:560px;padding:24px">
        <div style="font-size:18px;font-weight:600;margin-bottom:10px">\(safeTitle)</div>
        <pre style="white-space:pre-wrap;color:#8d95a5;font:12px ui-monospace,Menlo,monospace;text-align:left">\(safeDetail)</pre>
        </div></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // MARK: menu

    func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Про YT Grab", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Сховати YT Grab", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "Сховати інші", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "Показати всі", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Вийти з YT Grab", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Редагування")
        editMenu.addItem(withTitle: "Скасувати", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = editMenu.addItem(withTitle: "Повторити", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Вирізати", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Копіювати", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Вставити", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Виділити все", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let viewMenu = NSMenu(title: "Вигляд")
        viewMenu.addItem(withTitle: "Оновити", action: #selector(reloadPage), keyEquivalent: "r")
        viewItem.submenu = viewMenu
        main.addItem(viewItem)

        let windowItem = NSMenuItem()
        let windowMenu = NSMenu(title: "Вікно")
        windowMenu.addItem(withTitle: "Згорнути", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "Масштабувати", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        windowItem.submenu = windowMenu
        main.addItem(windowItem)
        NSApp.windowsMenu = windowMenu

        NSApp.mainMenu = main
    }

    @objc func reloadPage() {
        if let url = serverURL {
            webView.load(URLRequest(url: url))
        }
    }

    // MARK: Python server

    func findPython() -> String? {
        let candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    func startServer() {
        guard let resources = Bundle.main.resourceURL?.appendingPathComponent("ytgrab"),
              FileManager.default.fileExists(atPath: resources.appendingPathComponent("app.py").path) else {
            showMessage("Не знайдено файли застосунку", detail: "Перезбери YT Grab командою build-app.sh")
            return
        }
        let home = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".ytgrab")
        let bundledUV = Bundle.main.bundleURL.appendingPathComponent("Contents/MacOS/uv")
        let hasUV = FileManager.default.isExecutableFile(atPath: bundledUV.path)
        let python = findPython()
        if !hasUV && python == nil {
            showMessage("Потрібен Python 3", detail: "Відкрий Термінал і виконай:\nbrew install python")
            return
        }

        try? FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        let logURL = home.appendingPathComponent("app.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        logHandle = try? FileHandle(forWritingTo: logURL)

        let appScript = resources.appendingPathComponent("app.py").path
        let process = Process()
        var env = ProcessInfo.processInfo.environment
        if hasUV {
            // The bundled uv fetches a private Python on first launch, so the Mac needs nothing preinstalled.
            process.executableURL = bundledUV
            process.arguments = ["run", "--quiet", "--no-project", "--python", "3.12", appScript]
            env["YTGRAB_UV"] = bundledUV.path
            env["UV_PYTHON_INSTALL_DIR"] = home.appendingPathComponent("python").path
            env["UV_CACHE_DIR"] = home.appendingPathComponent("uv-cache").path
            showMessage("Запускаю YT Grab…", detail: "Перший запуск готує Python і може тривати до хвилини.")
        } else {
            process.executableURL = URL(fileURLWithPath: python!)
            process.arguments = [appScript]
        }
        process.currentDirectoryURL = resources
        let basePath = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PATH"] = basePath + ":" + (env["PATH"] ?? "")
        env["YTGRAB_NO_BROWSER"] = "1"
        env["YTGRAB_BUNDLED"] = "1"
        env["YTGRAB_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"  // never write into the signed app bundle
        process.environment = env

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            DispatchQueue.main.async { self?.handleServerOutput(text, raw: data) }
        }
        process.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async { self?.serverExited(code: proc.terminationStatus) }
        }

        do {
            try process.run()
            server = process
        } catch {
            showMessage("Не вдалося запустити Python", detail: error.localizedDescription)
        }
    }

    func handleServerOutput(_ text: String, raw: Data) {
        logHandle?.write(raw)
        guard serverURL == nil else { return }
        serverOutput += text
        if let range = serverOutput.range(of: "http://127\\.0\\.0\\.1:[0-9]+/", options: .regularExpression),
           let url = URL(string: String(serverOutput[range])) {
            serverURL = url
            webView.load(URLRequest(url: url))
        }
    }

    func serverExited(code: Int32) {
        server = nil
        guard !quitting else { return }
        let tail = serverOutput.split(separator: "\n").suffix(15).joined(separator: "\n")
        serverURL = nil
        showMessage("Рушій YT Grab зупинився (код \(code))",
                    detail: tail.isEmpty ? "Журнал: ~/.ytgrab/app.log" : tail + "\n\nЖурнал: ~/.ytgrab/app.log")
    }

    func stopServer() {
        guard let process = server, process.isRunning else { return }
        process.interrupt()  // SIGINT: the server stops its jobs (yt-dlp, ffmpeg) before exiting
        let deadline = Date().addingTimeInterval(3)
        while process.isRunning && Date() < deadline {
            usleep(50_000)
        }
        if process.isRunning {
            process.terminate()
        }
    }

    // MARK: page bridge

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let cmd = body["cmd"] as? String else { return }
        let id = (body["id"] as? NSNumber)?.intValue ?? 0
        switch cmd {
        case "choose":
            let panel = NSOpenPanel()
            panel.allowsMultipleSelection = true
            panel.canChooseDirectories = true
            panel.canChooseFiles = true
            panel.prompt = "Додати"
            panel.message = "Обери відео, аудіо або цілі папки"
            panel.beginSheetModal(for: window) { response in
                let paths = response == .OK ? panel.urls.map { $0.path } : []
                self.reply(id, paths)
            }
        case "paste":
            reply(id, NSPasteboard.general.string(forType: .string) ?? "")
        default:
            reply(id, NSNull())
        }
    }

    func reply(_ id: Int, _ payload: Any) {
        // Wrapped in an array so plain strings serialize as valid JSON.
        guard let data = try? JSONSerialization.data(withJSONObject: [payload]),
              let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.ytgrabNative.resolve(\(id), \(json)[0])", completionHandler: nil)
    }

    // MARK: WKUIDelegate / WKNavigationDelegate

    // <input type="file"> (the ".txt with links" picker).
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.beginSheetModal(for: window) { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }

    // target="_blank" links open in the default browser.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = navigationAction.request.url, navigationAction.navigationType == .linkActivated,
           url.host != "127.0.0.1", url.scheme == "http" || url.scheme == "https" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
