// mic_watch.m — who is using the microphone right now?
//
// Prints the names of processes currently running audio INPUT (the same
// signal as the menu-bar orange dot), one per line; prints nothing when the
// mic is idle. Uses the CoreAudio process-object API (macOS 14.4+) — this is
// device METADATA, so it needs no TCC permission at all.
//
// This is the Granola trigger: capture follows the CALL (a meeting app
// holding the mic), not the calendar. meet_watch.sh polls this to decide
// start/stop; the calendar is only used to label the recording.
//
// Build: clang -fobjc-arc -O2 mic_watch.m -o mic_watch \
//          -framework CoreAudio -framework Foundation
#import <CoreAudio/AudioHardware.h>
#import <Foundation/Foundation.h>
#include <libproc.h>

int main(void) {
    @autoreleasepool {
        AudioObjectPropertyAddress listAddr = {
            kAudioHardwarePropertyProcessObjectList,
            kAudioObjectPropertyScopeGlobal,
            kAudioObjectPropertyElementMain,
        };
        UInt32 size = 0;
        if (AudioObjectGetPropertyDataSize(kAudioObjectSystemObject, &listAddr, 0, NULL, &size) != noErr)
            return 1;
        UInt32 count = size / sizeof(AudioObjectID);
        if (count == 0) return 0;
        AudioObjectID *procs = calloc(count, sizeof(AudioObjectID));
        if (AudioObjectGetPropertyData(kAudioObjectSystemObject, &listAddr, 0, NULL, &size, procs) != noErr) {
            free(procs);
            return 1;
        }
        count = size / sizeof(AudioObjectID);

        AudioObjectPropertyAddress inputAddr = {
            kAudioProcessPropertyIsRunningInput,
            kAudioObjectPropertyScopeGlobal,
            kAudioObjectPropertyElementMain,
        };
        AudioObjectPropertyAddress pidAddr = {
            kAudioProcessPropertyPID,
            kAudioObjectPropertyScopeGlobal,
            kAudioObjectPropertyElementMain,
        };
        for (UInt32 i = 0; i < count; i++) {
            UInt32 running = 0, rs = sizeof(running);
            if (AudioObjectGetPropertyData(procs[i], &inputAddr, 0, NULL, &rs, &running) != noErr || !running)
                continue;
            pid_t pid = -1;
            UInt32 ps = sizeof(pid);
            if (AudioObjectGetPropertyData(procs[i], &pidAddr, 0, NULL, &ps, &pid) != noErr || pid <= 0)
                continue;
            char path[PROC_PIDPATHINFO_MAXSIZE] = {0};
            if (proc_pidpath(pid, path, sizeof(path)) <= 0)
                continue;
            const char *name = strrchr(path, '/');
            printf("%d %s\n", pid, name ? name + 1 : path);
        }
        free(procs);
    }
    return 0;
}
