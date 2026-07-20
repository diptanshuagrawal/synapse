// meet_record_capture.m — dual-stream meeting audio capture for macOS.
//
// Captures BOTH sides of a call without any virtual-audio driver:
//   - system audio (everyone else) via ScreenCaptureKit
//   - microphone (you) via AVAudioEngine
// Each stream is written to its own WAV; the meet-record wrapper mixes them
// with ffmpeg at stop time (offline mix avoids realtime clock-sync issues).
//
// Objective-C (not Swift) on purpose: the standalone CommandLineTools ship a
// duplicated SwiftBridging modulemap that breaks any swiftc build importing
// system frameworks; clang + #import is immune.
//
// Usage:   meet-record-capture <system-out.wav> <mic-out.wav>
// Stop:    SIGINT / SIGTERM → finalize both files and exit 0.
// Perms:   Screen Recording (system audio) + Microphone — one-time TCC
//          prompts against the app that launches it (Terminal / Shortcuts).
//
// Build (done by the meet-record wrapper when missing/stale):
//   clang -fobjc-arc -O2 meet_record_capture.m -o meet-record-capture \
//     -framework Foundation -framework AVFoundation -framework CoreMedia \
//     -framework ScreenCaptureKit

#import <AVFoundation/AVFoundation.h>
#import <CoreMedia/CoreMedia.h>
#import <Foundation/Foundation.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#include <fcntl.h>
#include <sys/time.h>

// Voice-activity sidecar: whenever captured audio is above a speech-ish level,
// touch <capture-dir>/voice_active (throttled to once per 5s). The auto-stop
// logic uses its mtime to keep recording through meetings that run past their
// calendar end — stop only once the room has actually gone quiet.
static NSString *gVoiceFile = nil;
static double gLastVoiceTouch = 0;

static void NoteVoiceLevel(AVAudioPCMBuffer *pcm) {
    if (!gVoiceFile || !pcm.floatChannelData || pcm.frameLength == 0) return;
    double now = [NSDate date].timeIntervalSince1970;
    if (now - gLastVoiceTouch < 5.0) return;
    const float *ch = pcm.floatChannelData[0];
    double acc = 0;
    AVAudioFrameCount n = pcm.frameLength, step = n > 512 ? n / 512 : 1, cnt = 0;
    for (AVAudioFrameCount i = 0; i < n; i += step) { acc += fabsf(ch[i]); cnt++; }
    if (cnt && (acc / cnt) > 0.008) {  // ~speech level; ambient hiss stays below
        gLastVoiceTouch = now;
        int fd = open(gVoiceFile.UTF8String, O_WRONLY | O_CREAT, 0644);
        if (fd >= 0) { futimes(fd, NULL); close(fd); }
    }
}

static AVAudioPCMBuffer *PCMBufferFromSampleBuffer(CMSampleBufferRef sb) {
    CMFormatDescriptionRef fmt = CMSampleBufferGetFormatDescription(sb);
    if (!fmt) return nil;
    const AudioStreamBasicDescription *asbd =
        CMAudioFormatDescriptionGetStreamBasicDescription(fmt);
    if (!asbd) return nil;
    AVAudioFormat *format = [[AVAudioFormat alloc] initWithStreamDescription:asbd];
    if (!format) return nil;
    CMItemCount frames = CMSampleBufferGetNumSamples(sb);
    if (frames <= 0) return nil;
    AVAudioPCMBuffer *pcm =
        [[AVAudioPCMBuffer alloc] initWithPCMFormat:format
                                      frameCapacity:(AVAudioFrameCount)frames];
    if (!pcm) return nil;
    pcm.frameLength = (AVAudioFrameCount)frames;
    OSStatus st = CMSampleBufferCopyPCMDataIntoAudioBufferList(
        sb, 0, (int32_t)frames, pcm.mutableAudioBufferList);
    return st == noErr ? pcm : nil;
}

@interface SystemAudioSink : NSObject <SCStreamOutput>
@property(nonatomic, strong) NSURL *url;
@property(nonatomic, strong) AVAudioFile *file;
@property(atomic) long long frames;
@end

@implementation SystemAudioSink
- (void)stream:(SCStream *)stream
    didOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer
                   ofType:(SCStreamOutputType)type {
    if (type != SCStreamOutputTypeAudio || !CMSampleBufferIsValid(sampleBuffer)) return;
    AVAudioPCMBuffer *pcm = PCMBufferFromSampleBuffer(sampleBuffer);
    if (!pcm) return;
    NSError *err = nil;
    if (!self.file) {
        // Lazily create with the stream's real format (48 kHz stereo float).
        self.file = [[AVAudioFile alloc] initForWriting:self.url
                                               settings:pcm.format.settings
                                                  error:&err];
        if (err) { fprintf(stderr, "system wav create error: %s\n", err.description.UTF8String); return; }
    }
    if (![self.file writeFromBuffer:pcm error:&err])
        fprintf(stderr, "system wav write error: %s\n", err.description.UTF8String);
    else {
        self.frames += pcm.frameLength;
        NoteVoiceLevel(pcm);
    }
}
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 3) {
            fprintf(stderr, "usage: meet-record-capture <system-out.wav> <mic-out.wav>\n");
            return 2;
        }
        NSURL *sysURL = [NSURL fileURLWithPath:@(argv[1])];
        NSURL *micURL = [NSURL fileURLWithPath:@(argv[2])];
        gVoiceFile = [sysURL.URLByDeletingLastPathComponent
                         URLByAppendingPathComponent:@"voice_active"].path;

        SystemAudioSink *sink = [SystemAudioSink new];
        sink.url = sysURL;

        // --- microphone -----------------------------------------------------
        AVAudioEngine *engine = [AVAudioEngine new];
        __block AVAudioFile *micFile = nil;
        @try {
            AVAudioInputNode *input = engine.inputNode;
            AVAudioFormat *fmt = [input outputFormatForBus:0];
            NSError *err = nil;
            micFile = [[AVAudioFile alloc] initForWriting:micURL settings:fmt.settings error:&err];
            if (!err) {
                [input installTapOnBus:0 bufferSize:4096 format:fmt
                                 block:^(AVAudioPCMBuffer *buf, AVAudioTime *when) {
                                   NSError *werr = nil;
                                   [micFile writeFromBuffer:buf error:&werr];
                                   NoteVoiceLevel(buf);
                                 }];
                if (![engine startAndReturnError:&err]) {
                    fprintf(stderr, "mic unavailable: %s — continuing system-audio only\n",
                            err.description.UTF8String);
                    micFile = nil;
                }
            }
        } @catch (NSException *ex) {
            fprintf(stderr, "mic unavailable: %s — continuing system-audio only\n",
                    ex.description.UTF8String);
            micFile = nil;
        }

        // --- system audio (ScreenCaptureKit) --------------------------------
        dispatch_queue_t audioQueue = dispatch_queue_create("meet-record.audio", NULL);
        dispatch_semaphore_t startSem = dispatch_semaphore_create(0);
        __block SCStream *stream = nil;
        __block NSError *startError = nil;

        [SCShareableContent getShareableContentExcludingDesktopWindows:NO
                                                   onScreenWindowsOnly:NO
                                                     completionHandler:^(SCShareableContent *content, NSError *error) {
            if (error || content.displays.count == 0) {
                startError = error ?: [NSError errorWithDomain:@"meet-record" code:1
                    userInfo:@{NSLocalizedDescriptionKey : @"no display found"}];
                dispatch_semaphore_signal(startSem);
                return;
            }
            SCContentFilter *filter =
                [[SCContentFilter alloc] initWithDisplay:content.displays.firstObject
                                        excludingWindows:@[]];
            SCStreamConfiguration *cfg = [SCStreamConfiguration new];
            cfg.capturesAudio = YES;
            cfg.excludesCurrentProcessAudio = YES;
            // Video is mandatory plumbing for SCStream; make it as cheap as possible.
            cfg.width = 2;
            cfg.height = 2;
            cfg.minimumFrameInterval = CMTimeMake(1, 1);
            stream = [[SCStream alloc] initWithFilter:filter configuration:cfg delegate:nil];
            NSError *addErr = nil;
            [stream addStreamOutput:sink
                               type:SCStreamOutputTypeAudio
                 sampleHandlerQueue:audioQueue
                              error:&addErr];
            if (addErr) {
                startError = addErr;
                dispatch_semaphore_signal(startSem);
                return;
            }
            [stream startCaptureWithCompletionHandler:^(NSError *serr) {
                startError = serr;
                dispatch_semaphore_signal(startSem);
            }];
        }];

        dispatch_semaphore_wait(startSem, dispatch_time(DISPATCH_TIME_NOW, 15 * NSEC_PER_SEC));
        if (startError) {
            // Screen Recording TCC denied (admin-gated on managed Macs). Fall
            // back to microphone-only — with speakers on, the mic still hears
            // both sides. The day the permission lands this branch stops firing
            // and system audio joins automatically. Only give up if the mic is
            // dead too.
            fprintf(stderr, "WARN: system-audio capture unavailable (%s) — falling back to mic-only\n",
                    startError.localizedDescription.UTF8String);
            stream = nil;
            if (!micFile) {
                fprintf(stderr, "FATAL: neither system audio nor microphone available\n");
                [engine stop];
                return 1;
            }
        }
        printf("RECORDING%s sys=%s mic=%s\n", stream ? "" : " (mic-only)", argv[1], argv[2]);
        fflush(stdout);

        // --- graceful shutdown on SIGINT/SIGTERM -----------------------------
        __block BOOL shuttingDown = NO;
        void (^shutdown)(void) = ^{
            if (shuttingDown) return;
            shuttingDown = YES;
            if (stream) {
                dispatch_semaphore_t stopSem = dispatch_semaphore_create(0);
                [stream stopCaptureWithCompletionHandler:^(NSError *e) {
                    dispatch_semaphore_signal(stopSem);
                }];
                dispatch_semaphore_wait(stopSem, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC));
            }
            [engine.inputNode removeTapOnBus:0];
            [engine stop];
            // AVAudioFile finalizes its WAV header when closed/deallocated.
            dispatch_sync(audioQueue, ^{
                if ([sink.file respondsToSelector:@selector(close)]) [sink.file close];
                sink.file = nil;
            });
            if ([micFile respondsToSelector:@selector(close)]) [micFile close];
            micFile = nil;
            printf("STOPPED frames=%lld\n", sink.frames);
            exit(0);
        };

        signal(SIGINT, SIG_IGN);
        signal(SIGTERM, SIG_IGN);
        dispatch_source_t sigint = dispatch_source_create(
            DISPATCH_SOURCE_TYPE_SIGNAL, SIGINT, 0, dispatch_get_main_queue());
        dispatch_source_set_event_handler(sigint, shutdown);
        dispatch_resume(sigint);
        dispatch_source_t sigterm = dispatch_source_create(
            DISPATCH_SOURCE_TYPE_SIGNAL, SIGTERM, 0, dispatch_get_main_queue());
        dispatch_source_set_event_handler(sigterm, shutdown);
        dispatch_resume(sigterm);

        dispatch_main();
    }
    return 0;
}
