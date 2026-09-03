import AppKit
import SwiftUI

extension Notification.Name {
    static let doneGuardShowCompact = Notification.Name("DoneGuardShowCompact")
    static let doneGuardShowDetails = Notification.Name("DoneGuardShowDetails")
    static let doneGuardHide = Notification.Name("DoneGuardHide")
}

struct DisplayCheck: Codable, Equatable, Identifiable {
    let title: String
    let status: String
    let detail: String

    var id: String { title }
}

struct DisplayFinding: Codable, Equatable, Identifiable {
    let title: String
    let detail: String
    let nextStep: String
    let technicalDetail: String

    var id: String { title + technicalDetail }

    enum CodingKeys: String, CodingKey {
        case title
        case detail
        case nextStep = "next_step"
        case technicalDetail = "technical_detail"
    }
}

struct ReportDisplay: Codable, Equatable {
    let headline: String
    let summary: String
    let modeLabel: String
    let checks: [DisplayCheck]
    let blockers: [DisplayFinding]
    let warnings: [DisplayFinding]
    let passed: [DisplayFinding]
    let filesSummary: String

    enum CodingKeys: String, CodingKey {
        case headline
        case summary
        case modeLabel = "mode_label"
        case checks
        case blockers
        case warnings
        case passed
        case filesSummary = "files_summary"
    }
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
    let display: ReportDisplay?

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
        case display
    }

    var headline: String {
        if let display { return display.headline }
        if !blockers.isEmpty { return "暂时还不能确认任务已完成" }
        if !warnings.isEmpty { return "任务已有完成证据，但还有提醒" }
        return "任务已完成检查"
    }

    var plainSummary: String {
        if let display { return display.summary }
        if !blockers.isEmpty { return "发现 \(blockers.count) 个需要处理的问题。打开报告可以查看原因和建议。" }
        if !warnings.isEmpty { return "没有发现阻断问题，同时有 \(warnings.count) 项内容建议你确认。" }
        return "没有发现需要阻止交付的问题。"
    }

    var modeLabel: String {
        if let display { return display.modeLabel }
        switch mode {
        case "strict": return "严格模式（证据不足时会让 Codex 再检查一次）"
        case "observe": return "观察模式（只记录，不弹出提醒）"
        default: return "提醒模式（只提示，不阻止任务结束）"
        }
    }

    var checkedAtLabel: String {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = parser.date(from: checkedAt)
        if date == nil {
            parser.formatOptions = [.withInternetDateTime]
            date = parser.date(from: checkedAt)
        }
        guard let date else { return checkedAt }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.timeZone = .current
        formatter.dateFormat = "yyyy年M月d日 HH:mm"
        return formatter.string(from: date)
    }

    private func legacyFindings(_ values: [String], category: String) -> [DisplayFinding] {
        values.enumerated().map { index, value in
            let title: String
            let detail: String
            let nextStep: String
            switch category {
            case "blocker":
                title = "问题 \(index + 1) 需要处理"
                detail = "这项检查没有满足当前项目的完成要求。"
                nextStep = "请把技术详情交给 Codex 或开发者处理，然后重新检查。"
            case "warning":
                title = "提醒 \(index + 1)"
                detail = "这项内容不会阻止任务结束，但建议交付前确认。"
                nextStep = "如果不确定是否有影响，可以请 Codex 进一步检查。"
            default:
                title = "已确认项目 \(index + 1)"
                detail = "DoneGuard 找到了支持任务完成的检查证据。"
                nextStep = ""
            }
            return DisplayFinding(title: title, detail: detail, nextStep: nextStep, technicalDetail: value)
        }
    }

    var displayBlockers: [DisplayFinding] { display?.blockers ?? legacyFindings(blockers, category: "blocker") }
    var displayWarnings: [DisplayFinding] { display?.warnings ?? legacyFindings(warnings, category: "warning") }
    var displayPassed: [DisplayFinding] { display?.passed ?? legacyFindings(passed, category: "passed") }
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
                Text(report.headline)
                    .font(.system(size: 18, weight: .bold, design: .rounded))
                    .lineLimit(2)
                Text(report.plainSummary)
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

struct CheckOverview: View {
    let checks: [DisplayCheck]

    private func color(for status: String) -> Color {
        switch status {
        case "issue": return .red
        case "warning": return .orange
        case "passed": return .green
        default: return .secondary
        }
    }

    private func icon(for status: String) -> String {
        switch status {
        case "issue": return "xmark.circle.fill"
        case "warning": return "exclamationmark.triangle.fill"
        case "passed": return "checkmark.circle.fill"
        default: return "minus.circle.fill"
        }
    }

    var body: some View {
        if !checks.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                Text("DoneGuard 检查了什么")
                    .font(.headline)
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                    ForEach(checks) { check in
                        HStack(alignment: .top, spacing: 9) {
                            Image(systemName: icon(for: check.status))
                                .foregroundStyle(color(for: check.status))
                                .padding(.top, 2)
                            VStack(alignment: .leading, spacing: 4) {
                                Text(check.title).font(.subheadline.bold())
                                Text(check.detail)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer(minLength: 0)
                        }
                        .padding(12)
                        .frame(maxWidth: .infinity, minHeight: 82, alignment: .topLeading)
                        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
                        .overlay(alignment: .leading) {
                            RoundedRectangle(cornerRadius: 12)
                                .fill(color(for: check.status))
                                .frame(width: 3)
                        }
                    }
                }
            }
            .padding(16)
            .background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 14))
        }
    }
}

struct FindingSection: View {
    let title: String
    let icon: String
    let color: Color
    let values: [DisplayFinding]

    var body: some View {
        if !values.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                Label(title, systemImage: icon)
                    .font(.headline)
                    .foregroundStyle(color)
                ForEach(values) { value in
                    HStack(alignment: .top, spacing: 9) {
                        Circle().fill(color).frame(width: 6, height: 6).padding(.top, 7)
                        VStack(alignment: .leading, spacing: 6) {
                            Text(value.title).font(.subheadline.bold())
                            Text(value.detail)
                                .fixedSize(horizontal: false, vertical: true)
                            if !value.nextStep.isEmpty {
                                HStack(alignment: .top, spacing: 5) {
                                    Text("建议").font(.caption.bold()).foregroundStyle(color)
                                    Text(value.nextStep).font(.callout)
                                }
                            }
                            if !value.technicalDetail.isEmpty {
                                DisclosureGroup("查看技术详情") {
                                    Text(value.technicalDetail)
                                        .font(.system(.caption, design: .monospaced))
                                        .foregroundStyle(.secondary)
                                        .textSelection(.enabled)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                        .padding(.top, 4)
                                }
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            }
                        }
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
                            Text("检查时间  \(report.checkedAtLabel)").font(.caption).foregroundStyle(.secondary)
                            Text(report.modeLabel).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    VStack(alignment: .leading, spacing: 7) {
                        Text("检查结论").font(.caption.bold()).foregroundStyle(.secondary)
                        Text(report.headline).font(.title3.bold())
                        Text(report.plainSummary)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(17)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.accentColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 14))

                    CheckOverview(checks: report.display?.checks ?? [])
                    FindingSection(title: "为什么暂时不能确认完成", icon: "exclamationmark.octagon.fill", color: .red, values: report.displayBlockers)
                    FindingSection(title: "还有这些内容值得留意", icon: "exclamationmark.triangle.fill", color: .orange, values: report.displayWarnings)
                    FindingSection(title: "已经确认的内容", icon: "checkmark.seal.fill", color: .green, values: report.displayPassed)
                    VStack(alignment: .leading, spacing: 10) {
                        Label("本次检查涉及的文件", systemImage: "doc.on.doc")
                            .font(.headline)
                            .foregroundStyle(.blue)
                        Text(report.display?.filesSummary ?? (report.changedPaths.isEmpty ? "本次没有发现需要验证的项目改动。" : "本次共检查 \(report.changedPaths.count) 个相关文件。"))
                            .foregroundStyle(.secondary)
                        ForEach(report.changedPaths, id: \.self) { path in
                            Text(path)
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                        }
                    }
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.blue.opacity(0.07), in: RoundedRectangle(cornerRadius: 14))
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
