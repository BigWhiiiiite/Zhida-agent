// Read-only local bridge. No clicks, input, permission requests, or desktop capture.
import Foundation
import AppKit
import ApplicationServices
import ScreenCaptureKit
import ImageIO
import UniformTypeIdentifiers

func attribute(_ e: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(e, name as CFString, &value) == .success ? value : nil
}

func frame(_ e: AXUIElement) -> CGRect? {
    guard let p = attribute(e, kAXPositionAttribute), let s = attribute(e, kAXSizeAttribute),
          CFGetTypeID(p) == AXValueGetTypeID(), CFGetTypeID(s) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero, size = CGSize.zero
    guard AXValueGetValue(p as! AXValue, .cgPoint, &point),
          AXValueGetValue(s as! AXValue, .cgSize, &size) else { return nil }
    return CGRect(origin: point, size: size)
}

func children(_ e: AXUIElement) -> [AXUIElement] {
    attribute(e, kAXChildrenAttribute) as? [AXUIElement] ?? []
}

func sameFrame(_ a: CGRect, _ b: CGRect) -> Bool {
    abs(a.minX-b.minX)<3 && abs(a.minY-b.minY)<3 && abs(a.width-b.width)<3 && abs(a.height-b.height)<3
}

func emit(_ result: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: result) {
        FileHandle.standardOutput.write(data)
    }
}

@main struct Observer {
    static func main() async {
        do { try await observe() }
        catch { emit(["limitation": "macOS 窗口或画面未能安全读取；没有读取其他窗口。"]) }
    }

    static func observe() async throws {
        let input = FileHandle.standardInput.readDataToEndOfFile()
        guard let request = try JSONSerialization.jsonObject(with: input) as? [String: Any],
              let bounds = request["bounds"] as? [Double], bounds.count == 4,
              let url = request["url"] as? String,
              let viewport = request["viewport"] as? [String: Double],
              let crop = request["crop"] as? [String: Double],
              let vw = viewport["width"], let vh = viewport["height"], vw>0, vh>0 else { return }
        guard AXIsProcessTrusted() else {
            emit(["limitation": "Safari 原生可访问树需要 macOS 辅助功能授权；本工具不会主动请求或改变授权。"])
            return
        }
        let apps = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.Safari")
        guard apps.count == 1, let app = apps.first else { return }
        let expected = CGRect(x: bounds[0], y: bounds[1], width: bounds[2]-bounds[0], height: bounds[3]-bounds[1])
        let application = AXUIElementCreateApplication(app.processIdentifier)
        AXUIElementSetMessagingTimeout(application, 2)
        let windows = (attribute(application, kAXWindowsAttribute) as? [AXUIElement] ?? [])
            .filter { frame($0).map { sameFrame($0, expected) } ?? false }
        guard windows.count == 1 else {
            emit(["limitation": "无法唯一绑定已连接的 Safari 窗口，未读取画面。"]); return
        }
        // Examine only that window. Match the web area's exact URL, then demand
        // a viewport-sized AX scroll area; fail closed on ambiguous geometry.
        var queue: [(AXUIElement, CGRect?)] = [(windows[0], nil)]
        var areas: [(AXUIElement, CGRect)] = []
        var visited = 0
        while !queue.isEmpty && visited < 800 {
            let (node, ancestor) = queue.removeFirst(); visited += 1
            let role = attribute(node, kAXRoleAttribute) as? String ?? ""
            var scope = ancestor
            if role == "AXScrollArea", let r = frame(node) { scope = r.intersection(expected) }
            if role == "AXWebArea" {
                let raw = attribute(node, "AXURL")
                let address = (raw as? URL)?.absoluteString ?? raw as? String ?? ""
                if address == url, let r = scope ?? frame(node), !r.isNull,
                   expected.contains(r), abs(r.width/vw-r.height/vh)<0.07,
                   r.width/vw>0.5, r.width/vw<3 {
                    areas.append((node,r))
                }
                continue
            }
            queue.append(contentsOf: children(node).prefix(100).map { ($0,scope) })
        }
        guard areas.count == 1, let (web, visibleFrame) = areas.first else {
            emit(["limitation": "Safari 网页地址或可见区域未唯一核实，保留 DOM 证据，不截图。"]); return
        }
        let ratioX=visibleFrame.width/vw, ratioY=visibleFrame.height/vh
        let region=CGRect(x:visibleFrame.minX+(crop["x"] ?? 0)*ratioX,
                          y:visibleFrame.minY+(crop["y"] ?? 0)*ratioY,
                          width:(crop["width"] ?? 0)*ratioX,height:(crop["height"] ?? 0)*ratioY)
        guard visibleFrame.contains(region), region.width>0, region.height>0 else { return }
        var nodes: [[String: Any]]=[]
        var pending=[web]; visited=0
        let excluded: Set<String>=["AXTextField","AXTextArea","AXSecureTextField","AXComboBox","AXImage"]
        while !pending.isEmpty && visited<800 && nodes.count<100 {
            let node=pending.removeFirst(); visited += 1
            let role=attribute(node,kAXRoleAttribute) as? String ?? ""
            if let r=frame(node), r.intersects(region), !excluded.contains(role) {
                // Never read AXValue, selected text, account data, or cookies.
                let title=attribute(node,kAXTitleAttribute) as? String ?? ""
                let description=attribute(node,kAXDescriptionAttribute) as? String ?? ""
                nodes.append(["role":role,"name":String((title.isEmpty ? description:title).prefix(400))])
            }
            if !excluded.contains(role) { pending.append(contentsOf:children(node).prefix(100)) }
        }
        var result: [String:Any]=["accessibility":nodes]
        if request["include_image"] as? Bool == true {
            guard CGPreflightScreenCaptureAccess() else {
                result["limitation"]="Safari 截图需要 macOS 屏幕录制授权；本工具不会主动请求或改变授权。"
                emit(result); return
            }
            if #available(macOS 14.0, *) {
                let content=try await SCShareableContent.excludingDesktopWindows(true,onScreenWindowsOnly:true)
                let matches=content.windows.filter {
                    $0.owningApplication?.processID == app.processIdentifier && sameFrame($0.frame,expected)
                }
                guard matches.count == 1 else {
                    result["limitation"]="截图窗口映射不唯一，没有捕获整个桌面。"; emit(result); return
                }
                let filter=SCContentFilter(desktopIndependentWindow:matches[0])
                let config=SCStreamConfiguration()
                config.sourceRect=region.offsetBy(dx:-expected.minX,dy:-expected.minY)
                config.width=Int(region.width.rounded()); config.height=Int(region.height.rounded())
                config.showsCursor=false
                config.ignoreShadowsSingleWindow=true
                let image=try await SCScreenshotManager.captureImage(contentFilter:filter,configuration:config)
                let data=NSMutableData()
                guard let destination=CGImageDestinationCreateWithData(data,UTType.png.identifier as CFString,1,nil) else { return }
                CGImageDestinationAddImage(destination,image,nil)
                guard CGImageDestinationFinalize(destination) else { return }
                result["image_base64"]=(data as Data).base64EncodedString()
            } else { result["limitation"]="Safari 截图观察需要 macOS 14 或更新版本。" }
        }
        guard frame(windows[0]).map({sameFrame($0,expected)}) == true else { return }
        emit(result)
    }
}
