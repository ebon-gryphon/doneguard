import Foundation

@main
struct ReportStorageSmoke {
    static func main() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("doneguard-storage-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let bundle = root.appendingPathComponent("reports/temporary/example", isDirectory: true)
        try FileManager.default.createDirectory(at: bundle, withIntermediateDirectories: true)
        let report = bundle.appendingPathComponent("report.json")
        try Data("{}".utf8).write(to: report)

        try ReportStorage.discard(reportPath: report, dataDirectory: root)
        guard !FileManager.default.fileExists(atPath: bundle.path) else {
            fatalError("temporary report bundle still exists after discard")
        }
        print("ReportStorage discard smoke test passed")
    }
}
