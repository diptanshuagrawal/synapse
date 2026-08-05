// steno-agenda — read the local EventKit calendar (whatever accounts macOS
// Calendar syncs) and emit an occurrence-expanded ICS on stdout. Replaces the
// IT-blocked published-ICS URL with a local, network-free source that
// calendar_feed.py parses unchanged.
//
//   steno-agenda [--back N] [--days M]   (default: now-2d .. now+30d)
//
// BUILD (compiled binary is gitignored — rebuild after any edit):
//   swiftc -O bin/steno_agenda.swift -o bin/steno-agenda \
//     -framework EventKit -framework Foundation \
//     -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist \
//     -Xlinker bin/steno-agenda.Info.plist
//   codesign --force --sign "<local-codesign-id>" --identifier com.steno.agenda bin/steno-agenda
// First run from a Terminal to grant Calendar access (System Settings → Privacy →
// Calendars); the grant is keyed to the com.steno.agenda signing identity.
//
// Occurrence-expanded: EventKit returns each recurring instance as its own
// event, so we emit standalone VEVENTs (no RRULE) — calendar_feed treats them
// as singles and its recurrence logic stays a no-op here.
import Foundation
import EventKit

let args = CommandLine.arguments
func argVal(_ flag: String, _ def: Int) -> Int {
    if let i = args.firstIndex(of: flag), i + 1 < args.count, let v = Int(args[i + 1]) { return v }
    return def
}
let backDays = argVal("--back", 2)
let fwdDays  = argVal("--days", 30)

let store = EKEventStore()
let sem = DispatchSemaphore(value: 0)
var granted = false
var accessErr: Error?

func emit() {
    let now = Date()
    let cal = Calendar.current
    let start = cal.date(byAdding: .day, value: -backDays, to: now)!
    let end   = cal.date(byAdding: .day, value:  fwdDays, to: now)!
    let pred = store.predicateForEvents(withStart: start, end: end, calendars: nil)
    let events = store.events(matching: pred)

    let utc = DateFormatter()
    utc.locale = Locale(identifier: "en_US_POSIX")
    utc.dateFormat = "yyyyMMdd'T'HHmmss'Z'"
    utc.timeZone = TimeZone(identifier: "UTC")
    let dOnly = DateFormatter()
    dOnly.locale = Locale(identifier: "en_US_POSIX")
    dOnly.dateFormat = "yyyyMMdd"
    dOnly.timeZone = TimeZone.current

    func esc(_ s: String) -> String {
        s.replacingOccurrences(of: "\\", with: "\\\\")
         .replacingOccurrences(of: ";", with: "\\;")
         .replacingOccurrences(of: ",", with: "\\,")
         .replacingOccurrences(of: "\r", with: " ")
         .replacingOccurrences(of: "\n", with: " ")
    }

    var out = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//steno-agenda//EN\r\n"
    for e in events {
        let uid = e.calendarItemExternalIdentifier ?? e.eventIdentifier ?? UUID().uuidString
        out += "BEGIN:VEVENT\r\n"
        out += "UID:\(uid)\r\n"
        out += "SUMMARY:\(esc(e.title ?? "(no title)"))\r\n"
        if e.isAllDay {
            out += "DTSTART;VALUE=DATE:\(dOnly.string(from: e.startDate))\r\n"
            out += "DTEND;VALUE=DATE:\(dOnly.string(from: e.endDate))\r\n"
        } else {
            out += "DTSTART:\(utc.string(from: e.startDate))\r\n"
            out += "DTEND:\(utc.string(from: e.endDate))\r\n"
        }
        out += (e.status == .canceled) ? "STATUS:CANCELLED\r\n" : "STATUS:CONFIRMED\r\n"
        // Join URL so calendar_feed's `teams` flag lights up for Teams meetings.
        var joinURL = e.url?.absoluteString
        if joinURL == nil || !(joinURL!.contains("teams.microsoft.com")) {
            let blob = (e.notes ?? "") + " " + (e.location ?? "")
            if let r = blob.range(of: "https://teams\\.microsoft\\.com[^\\s>\"']*",
                                  options: .regularExpression) {
                joinURL = String(blob[r])
            }
        }
        if let u = joinURL, !u.isEmpty { out += "URL:\(u)\r\n" }
        if let loc = e.location, !loc.isEmpty { out += "LOCATION:\(esc(loc))\r\n" }
        out += "END:VEVENT\r\n"
    }
    out += "END:VCALENDAR\r\n"
    FileHandle.standardOutput.write(out.data(using: .utf8)!)
}

if #available(macOS 14.0, *) {
    store.requestFullAccessToEvents { ok, err in granted = ok; accessErr = err; sem.signal() }
} else {
    store.requestAccess(to: .event) { ok, err in granted = ok; accessErr = err; sem.signal() }
}
sem.wait()
if !granted {
    FileHandle.standardError.write(
        "steno-agenda: calendar access not granted: \(accessErr?.localizedDescription ?? "denied")\n"
            .data(using: .utf8)!)
    exit(2)
}
emit()
