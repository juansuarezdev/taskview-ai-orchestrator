// TaskView banner for macOS. Clicking it opens the URL it carries (the task's kanban) instead of Script Editor,
// which is what `osascript display notification` does.
//   TaskViewNotify.app/Contents/MacOS/TaskViewNotify "<title>" "<body>" ["<url>"]
// Without arguments (macOS relaunches the app when a banner is clicked) it waits for the click, opens the URL, quits.
import AppKit
import UserNotifications

final class Delegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let args = Array(CommandLine.arguments.dropFirst())

    func applicationWillFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self  // must be set before launch ends to receive the click
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard let title = args.first else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 10) { NSApp.terminate(nil) }  // launched by a click
            return
        }
        let center = UNUserNotificationCenter.current()
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else {
                FileHandle.standardError.write("sin permiso de notificaciones\n".data(using: .utf8)!)
                DispatchQueue.main.async { NSApp.terminate(nil) }
                return
            }
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = self.args.count > 1 ? self.args[1] : ""
            content.sound = .default
            if self.args.count > 2, !self.args[2].isEmpty { content.userInfo = ["url": self.args[2]] }
            let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
            center.add(request) { _ in DispatchQueue.main.async { NSApp.terminate(nil) } }
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        if let s = response.notification.request.content.userInfo["url"] as? String, let url = URL(string: s) {
            NSWorkspace.shared.open(url)
        }
        completionHandler()
        DispatchQueue.main.async { NSApp.terminate(nil) }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
}

let app = NSApplication.shared
let delegate = Delegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
