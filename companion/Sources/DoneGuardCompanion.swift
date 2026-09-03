import AppKit
import SwiftUI

extension Notification.Name {
    static let doneGuardShowCompact = Notification.Name("DoneGuardShowCompact")
    static let doneGuardShowDetails = Notification.Name("DoneGuardShowDetails")
    static let doneGuardHide = Notification.Name("DoneGuardHide")
}

struct CompletionReport: Codable, Identifiable, Equatable {
    let reportID: String
    let projectName: String
    let checkedAt: String
    let status: String
    let mode: String
    let passed: [String]
    let warnings: [String]
    let blockers: [String]
    let changedPaths: [String]

    var id: String { reportID }

    enum CodingKeys: String, CodingKey {
        case reportID = "report_id"
        case projectName = "project_name"
        case checkedAt = "checked_at"
        case status
        case mode
        case passed
        case warnings
        case blockers
        case changedPaths = "changed_paths"
    }
}

struct ReportEvent: Codable {
    let reportID: String
    let reportPath: String

    enum CodingKeys: String, CodingKey {
        case reportID = "report_id"
        case reportPath = "report_path"
    }
}

enum ReportStorage {
    static func discard(reportPath: URL, dataDirectory: URL) throws {
        let bundle = reportPath.deletingLastPathComponent().standardizedFileURL
        let temporaryRoot = dataDirectory
            .appendingPathComponent("reports/temporary", isDirectory: true)
            .standardizedFileURL.path + "/"
        guard bundle.path.hasPrefix(temporaryRoot) else {
            throw CocoaError(.fileWriteNoPermission)
        }
        if FileManager.default.fileExists(atPath: bundle.path) {
            try FileManager.default.removeItem(at: bundle)
        }
    }
}

@MainActor
final class ReportStore: ObservableObject {
    @Published var report: CompletionReport?
    @Published var showingDetails = false
    @Published var errorMessage: String?

    private(set) var reportPath: URL?
    let dataDirectory: URL

    init() {
        let arguments = CommandLine.arguments
        if let flag = arguments.firstIndex(of: "--data-dir"), arguments.indices.contains(flag + 1) {
            dataDirectory = URL(fileURLWithPath: arguments[flag + 1], isDirectory: true)
        } else if let configured = ProcessInfo.processInfo.environment["PLUGIN_DATA"], !configured.isEmpty {
            dataDirectory = URL(fileURLWithPath: configured, isDirectory: true)
        } else {
            dataDirectory = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".codex/doneguard-data", isDirectory: true)
        }
        poll()
    }

    func poll() {
        guard report == nil else { return }
        let events = dataDirectory.appendingPathComponent("events", isDirectory: true)
        guard let candidates = try? FileManager.default.contentsOfDirectory(
            at: events,
            includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles]
        ) else { return }

        let ordered = candidates
            .filter { $0.pathExtension == "json" }
            .sorted {
                let left = (try? $0.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
                let right = (try? $1.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
                return left < right
            }
        guard let eventURL = ordered.first else { return }

        do {
            let event = try JSONDecoder().decode(ReportEvent.self, from: Data(contentsOf: eventURL))
            let candidate = URL(fileURLWithPath: event.reportPath)
            let temporaryRoot = dataDirectory
                .appendingPathComponent("reports/temporary", isDirectory: true)
                .standardizedFileURL.path + "/"
            guard candidate.standardizedFileURL.path.hasPrefix(temporaryRoot) else {
                throw CocoaError(.fileReadNoPermission)
            }
            let decoded = try JSONDecoder().decode(CompletionReport.self, from: Data(contentsOf: candidate))
            guard decoded.reportID == event.reportID else {
                throw CocoaError(.fileReadCorruptFile)
            }
            try? FileManager.default.removeItem(at: eventURL)
            reportPath = candidate
            report = decoded
            showingDetails = false
            errorMessage = nil
            NotificationCenter.default.post(name: .doneGuardShowCompact, object: nil)
        } catch {
            errorMessage = "报告暂时无法打开：\(error.localizedDescription)"
            try? FileManager.default.removeItem(at: eventURL)
            NotificationCenter.default.post(name: .doneGuardShowCompact, object: nil)
        }
    }

    func showDetails() {
        showingDetails = true
        NotificationCenter.default.post(name: .doneGuardShowDetails, object: nil)
    }

    func showSummary() {
        showingDetails = false
        NotificationCenter.default.post(name: .doneGuardShowCompact, object: nil)
    }

    func saveReport() {
        guard let report, let reportPath else { return }
        let source = reportPath.deletingLastPathComponent()
        let savedRoot = dataDirectory.appendingPathComponent("reports/saved", isDirectory: true)
        let destination = savedRoot.appendingPathComponent(report.reportID, isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: savedRoot, withIntermediateDirectories: true)
            if FileManager.default.fileExists(atPath: destination.path) {
                errorMessage = "这份报告已经保存过了。"
                return
            }
            try FileManager.default.moveItem(at: source, to: destination)
            finish()
        } catch {
            errorMessage = "保存失败：\(error.localizedDescription)"
        }
    }

    func discardReport() {
        let bundle = reportPath?.deletingLastPathComponent()
        NSLog("DoneGuard discard requested for %@", bundle?.lastPathComponent ?? "missing-report")
        report = nil
        reportPath = nil
        showingDetails = false
        errorMessage = nil
        NotificationCenter.default.post(name: .doneGuardHide, object: nil)

        guard let bundle else { return }
        do {
            try ReportStorage.discard(
                reportPath: bundle.appendingPathComponent("report.json"),
                dataDirectory: dataDirectory
            )
            NSLog("DoneGuard discarded temporary report %@", bundle.lastPathComponent)
        } catch {
            NSLog("DoneGuard could not discard temporary report: %@", error.localizedDescription)
            let alert = NSAlert()
            alert.messageText = "临时报告删除失败"
            alert.informativeText = error.localizedDescription
            alert.alertStyle = .warning
            alert.addButton(withTitle: "知道了")
            alert.runModal()
        }
    }

    func postpone() {
        NotificationCenter.default.post(name: .doneGuardHide, object: nil)
    }

    private func finish() {
        report = nil
        reportPath = nil
        showingDetails = false
        errorMessage = nil
        NotificationCenter.default.post(name: .doneGuardHide, object: nil)
    }
}

struct MascotImage: View {
    let status: String

    var body: some View {
        let name = status == "success" ? "mascot-success" : "mascot-issue"
        if let url = Bundle.main.url(forResource: name, withExtension: "png"),
           let image = NSImage(contentsOf: url) {
            Image(nsImage: image)
                .resizable()
                .scaledToFit()
        } else {
            Image(systemName: status == "success" ? "checkmark.seal.fill" : "magnifyingglass.circle.fill")
                .resizable()
                .scaledToFit()
                .foregroundStyle(status == "success" ? Color.green : Color.orange)
                .padding(40)
        }
    }
}

struct SummaryView: View {
    let report: CompletionReport
    let showDetails: () -> Void
    let postpone: () -> Void

    private var title: String {
        if !report.blockers.isEmpty { return "发现需要处理的问题" }
        if !report.warnings.isEmpty { return "任务完成，有几项提醒" }
        return "任务已完成"
    }

    private var summary: String {
        if !report.blockers.isEmpty {
            return "找到 \(report.blockers.count) 个需要处理的问题"
        }
        if !report.warnings.isEmpty {
            return "检查完成，同时留下 \(report.warnings.count) 项提醒"
        }
        return "没有发现阻断项"
    }

    private var accent: Color {
        report.status == "success" ? Color(red: 0.12, green: 0.55, blue: 0.43) : Color(red: 0.86, green: 0.47, blue: 0.10)
    }

    var body: some View {
        HStack(spacing: 13) {
            MascotImage(status: report.status)
                .frame(width: 88, height: 116)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 13) {
                HStack(spacing: 6) {
                    Circle().fill(accent).frame(width: 7, height: 7)
                    Text(report.projectName)
                        .font(.caption.weight(.semibold))
                        .lineLimit(1)
                        .foregroundStyle(.secondary)
                }
                Text(title)
                    .font(.system(size: 18, weight: .bold, design: .rounded))
                Text(summary)
                    .font(.system(size: 12.5))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)

                HStack(spacing: 8) {
                    Button("查看报告", action: showDetails)
                        .buttonStyle(.borderedProminent)
                        .tint(accent)
                        .controlSize(.small)
                    Button("稍后", action: postpone)
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(14)
        .frame(width: 400, height: 168)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .stroke(Color.primary.opacity(0.10), lineWidth: 1)
        }
    }
}

struct ReportSection: View {
    let title: String
    let icon: String
    let color: Color
    let values: [String]

    var body: some View {
        if !values.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                Label(title, systemImage: icon)
                    .font(.headline)
                    .foregroundStyle(color)
                ForEach(Array(values.enumerated()), id: \.offset) { _, value in
                    HStack(alignment: .top, spacing: 8) {
                        Circle().fill(color).frame(width: 5, height: 5).padding(.top, 8)
                        Text(value).textSelection(.enabled)
                    }
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(color.opacity(0.08), in: RoundedRectangle(cornerRadius: 14))
        }
    }
}

struct DetailView: View {
    let report: CompletionReport
    let back: () -> Void
    let save: () -> Void
    let discard: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Button(action: back) { Label("返回", systemImage: "chevron.left") }
                    .buttonStyle(.plain)
                Spacer()
                Text("DoneGuard 完整报告").font(.headline)
                Spacer()
                Color.clear.frame(width: 48, height: 1)
            }
            .padding(18)
            .background(Color(nsColor: .controlBackgroundColor))

            ScrollView {
                VStack(alignment: .leading, spacing: 15) {
                    HStack(spacing: 15) {
                        MascotImage(status: report.status).frame(width: 72, height: 72)
                        VStack(alignment: .leading, spacing: 5) {
                            Text(report.projectName).font(.title2.bold())
                            Text("检查时间  \(report.checkedAt)").font(.caption).foregroundStyle(.secondary)
                            Text("模式  \(report.mode)").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    ReportSection(title: "需要处理", icon: "exclamationmark.octagon.fill", color: .red, values: report.blockers)
                    ReportSection(title: "提醒", icon: "exclamationmark.triangle.fill", color: .orange, values: report.warnings)
                    ReportSection(title: "已通过", icon: "checkmark.seal.fill", color: .green, values: report.passed)
                    ReportSection(
                        title: "涉及文件",
                        icon: "doc.on.doc",
                        color: .blue,
                        values: report.changedPaths.isEmpty ? ["无相关文件变更"] : report.changedPaths
                    )
                    Text("DoneGuard 提供的是完成证据，不等同于需求正确性或完整测试覆盖。")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.top, 4)
                }
                .padding(24)
            }

            HStack {
                Button(role: .destructive, action: discard) {
                    Label("关闭且不保存", systemImage: "trash")
                }
                .buttonStyle(.borderedProminent)
                .tint(.red)
                Spacer()
                Text("只有点击保存，报告才会长期保留")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("保存报告", action: save)
                    .buttonStyle(.borderedProminent)
                    .tint(Color(red: 0.12, green: 0.55, blue: 0.43))
            }
            .padding(18)
            .background(Color(nsColor: .controlBackgroundColor))
        }
        .frame(width: 680, height: 720)
        .background(Color(nsColor: .windowBackgroundColor))
    }
}

struct ContentView: View {
    @ObservedObject var store: ReportStore
    private let poller = Timer.publish(every: 0.8, on: .main, in: .common).autoconnect()

    var body: some View {
        Group {
            if let report = store.report {
                if store.showingDetails {
                    DetailView(
                        report: report,
                        back: store.showSummary,
                        save: store.saveReport,
                        discard: store.discardReport
                    )
                } else {
                    SummaryView(
                        report: report,
                        showDetails: store.showDetails,
                        postpone: store.postpone
                    )
                }
            } else {
                VStack(spacing: 10) {
                    ProgressView()
                    Text(store.errorMessage ?? "等待 DoneGuard 报告…")
                        .foregroundStyle(.secondary)
                }
                .frame(width: 400, height: 168)
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            }
        }
        .onReceive(poller) { _ in store.poll() }
        .alert("DoneGuard", isPresented: Binding(
            get: { store.errorMessage != nil },
            set: { if !$0 { store.errorMessage = nil } }
        )) {
            Button("知道了") { store.errorMessage = nil }
        } message: {
            Text(store.errorMessage ?? "")
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let store = ReportStore()
    private var notificationPanel: NSPanel?
    private var detailWindow: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 400, height: 168),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.contentView = NSHostingView(rootView: ContentView(store: store))
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.isFloatingPanel = true
        panel.becomesKeyOnlyIfNeeded = true
        panel.hidesOnDeactivate = false
        panel.isMovableByWindowBackground = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        self.notificationPanel = panel

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(showCompact),
            name: .doneGuardShowCompact,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(showDetails),
            name: .doneGuardShowDetails,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(hidePanel),
            name: .doneGuardHide,
            object: nil
        )

        if store.report != nil || store.errorMessage != nil {
            if CommandLine.arguments.contains("--preview-details") && store.report != nil {
                store.showingDetails = true
                showDetails()
            } else {
                showCompact()
            }
        }
    }

    @objc private func showCompact() {
        guard let panel = notificationPanel else { return }
        detailWindow?.orderOut(nil)
        panel.level = .floating
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.setContentSize(NSSize(width: 400, height: 168))
        let screen = panel.screen ?? NSScreen.main ?? NSScreen.screens.first
        if let visible = screen?.visibleFrame {
            panel.setFrameOrigin(NSPoint(
                x: visible.maxX - panel.frame.width - 18,
                y: visible.maxY - panel.frame.height - 18
            ))
        }
        NSApp.unhideWithoutActivation()
        panel.orderFrontRegardless()
    }

    @objc private func showDetails() {
        notificationPanel?.orderOut(nil)
        let window: NSWindow
        if let existing = detailWindow {
            window = existing
        } else {
            let created = NSWindow(
                contentRect: NSRect(x: 0, y: 0, width: 680, height: 720),
                styleMask: [.titled, .closable, .fullSizeContentView],
                backing: .buffered,
                defer: false
            )
            created.contentView = NSHostingView(rootView: ContentView(store: store))
            created.titleVisibility = .hidden
            created.titlebarAppearsTransparent = true
            created.isMovableByWindowBackground = true
            created.backgroundColor = .windowBackgroundColor
            created.isOpaque = true
            created.hasShadow = true
            created.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            for kind in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
                created.standardWindowButton(kind)?.isHidden = true
            }
            detailWindow = created
            window = created
        }
        window.setContentSize(NSSize(width: 680, height: 720))
        window.center()
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    @objc private func hidePanel() {
        notificationPanel?.orderOut(nil)
        detailWindow?.orderOut(nil)
    }
}

#if !DONEGUARD_TESTING
@main
struct DoneGuardCompanionApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    init() {
        NSApplication.shared.setActivationPolicy(.accessory)
    }

    var body: some Scene {
        Settings {
            EmptyView()
        }
    }
}
#endif
