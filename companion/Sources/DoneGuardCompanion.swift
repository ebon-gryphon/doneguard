import AppKit
import SwiftUI

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
            NSApp.activate(ignoringOtherApps: true)
        } catch {
            errorMessage = "报告暂时无法打开：\(error.localizedDescription)"
            try? FileManager.default.removeItem(at: eventURL)
        }
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
        guard let reportPath else { return }
        do {
            try FileManager.default.removeItem(at: reportPath.deletingLastPathComponent())
            finish()
        } catch {
            errorMessage = "删除失败：\(error.localizedDescription)"
        }
    }

    func postpone() {
        NSApp.hide(nil)
    }

    private func finish() {
        report = nil
        reportPath = nil
        showingDetails = false
        errorMessage = nil
        NSApp.hide(nil)
    }
}

struct WindowStyler: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        DispatchQueue.main.async {
            guard let window = view.window else { return }
            window.level = .floating
            window.titleVisibility = .hidden
            window.titlebarAppearsTransparent = true
            window.isMovableByWindowBackground = true
            window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            window.center()
        }
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {}
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
            return "DoneGuard 找到 \(report.blockers.count) 个需要处理的问题。完整报告里有证据和涉及文件。"
        }
        if !report.warnings.isEmpty {
            return "检查已完成，同时留下 \(report.warnings.count) 项提醒，你可以打开报告再决定是否保存。"
        }
        return "没有发现阻断项。你可以查看完整证据，再决定是否保留这份报告。"
    }

    private var accent: Color {
        report.status == "success" ? Color(red: 0.12, green: 0.55, blue: 0.43) : Color(red: 0.86, green: 0.47, blue: 0.10)
    }

    var body: some View {
        VStack(spacing: 0) {
            ZStack(alignment: .topTrailing) {
                LinearGradient(
                    colors: report.status == "success"
                        ? [Color(red: 0.88, green: 0.97, blue: 0.92), Color(red: 0.97, green: 0.94, blue: 0.82)]
                        : [Color(red: 1.00, green: 0.94, blue: 0.80), Color(red: 1.00, green: 0.88, blue: 0.82)],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )
                MascotImage(status: report.status)
                    .padding(.top, 8)
                    .padding(.horizontal, 62)
                    .padding(.bottom, 2)
            }
            .frame(height: 292)

            VStack(alignment: .leading, spacing: 13) {
                Text(report.projectName.uppercased())
                    .font(.caption.weight(.bold))
                    .tracking(1.4)
                    .foregroundStyle(accent)
                Text(title)
                    .font(.system(size: 25, weight: .bold, design: .rounded))
                Text(summary)
                    .font(.system(size: 14.5))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                HStack(spacing: 10) {
                    Button("查看完整报告", action: showDetails)
                        .buttonStyle(.borderedProminent)
                        .tint(accent)
                        .controlSize(.large)
                    Button("稍后处理", action: postpone)
                        .buttonStyle(.bordered)
                        .controlSize(.large)
                }
            }
            .padding(25)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(nsColor: .windowBackgroundColor))
        }
        .frame(width: 460)
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
            .background(.thinMaterial)

            ScrollView {
                VStack(alignment: .leading, spacing: 15) {
                    HStack(spacing: 15) {
                        MascotImage(status: report.status).frame(width: 92, height: 92)
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
                Button("关闭且不保存", role: .destructive, action: discard)
                Spacer()
                Text("只有点击保存，报告才会长期保留")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("保存报告", action: save)
                    .buttonStyle(.borderedProminent)
                    .tint(Color(red: 0.12, green: 0.55, blue: 0.43))
            }
            .padding(18)
            .background(.thinMaterial)
        }
        .frame(width: 680, height: 720)
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
                        back: { store.showingDetails = false },
                        save: store.saveReport,
                        discard: store.discardReport
                    )
                } else {
                    SummaryView(
                        report: report,
                        showDetails: { store.showingDetails = true },
                        postpone: store.postpone
                    )
                }
            } else {
                VStack(spacing: 10) {
                    ProgressView()
                    Text(store.errorMessage ?? "等待 DoneGuard 报告…")
                        .foregroundStyle(.secondary)
                }
                .frame(width: 360, height: 180)
            }
        }
        .background(WindowStyler())
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

@main
struct DoneGuardCompanionApp: App {
    @StateObject private var store = ReportStore()

    init() {
        NSApplication.shared.setActivationPolicy(.accessory)
    }

    var body: some Scene {
        WindowGroup {
            ContentView(store: store)
        }
        .windowStyle(.hiddenTitleBar)
        .windowResizability(.contentSize)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}
