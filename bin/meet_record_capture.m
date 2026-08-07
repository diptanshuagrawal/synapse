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
//     -framework ScreenCaptureKit -framework CoreAudio

#import <AVFoundation/AVFoundation.h>
#import <CoreAudio/CoreAudio.h>
#import <CoreMedia/CoreMedia.h>
#import <Foundation/Foundation.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#include <fcntl.h>
#include <sys/time.h>

// Name of the current system default input device — logged at start and on
// every mic re-tap so a silent mic.wav can be traced to the device that was
// actually recorded (AirPods vs built-in was invisible before).
static NSString *DefaultInputName(void) {
    AudioDeviceID dev = 0;
    UInt32 sz = sizeof(dev);
    AudioObjectPropertyAddress addr = {kAudioHardwarePropertyDefaultInputDevice,
                                       kAudioObjectPropertyScopeGlobal,
                                       kAudioObjectPropertyElementMain};
    if (AudioObjectGetPropertyData(kAudioObjectSystemObject, &addr, 0, NULL, &sz, &dev) != noErr || !dev)
        return @"?";
    CFStringRef name = NULL;
    sz = sizeof(name);
    addr.mSelector = kAudioDevicePropertyDeviceNameCFString;
    if (AudioObjectGetPropertyData(dev, &addr, 0, NULL, &sz, &name) != noErr || !name)
        return @"?";
    return CFBridgingRelease(name);
}

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

// Mic-activity sidecar: touch <capture-dir>/mic_active whenever the MIC stream
// carries signal (throttled 5s). Mirrors NoteVoiceLevel but for the mic, so a
// DEAD/muted mic (silent recording — the on-speakers + mic-only failure) can be
// detected live by meet_ui, instead of surfacing days later as an empty note.
// Lower threshold than the system tap: room/speaker pickup is quieter than a
// direct app audio stream.
static NSString *gMicFile = nil;
static double gLastMicTouch = 0;

static void NoteMicLevel(AVAudioPCMBuffer *pcm) {
    if (!gMicFile || !pcm.floatChannelData || pcm.frameLength == 0) return;
    double now = [NSDate date].timeIntervalSince1970;
    if (now - gLastMicTouch < 5.0) return;
    const float *ch = pcm.floatChannelData[0];
    double acc = 0;
    AVAudioFrameCount n = pcm.frameLength, step = n > 512 ? n / 512 : 1, cnt = 0;
    for (AVAudioFrameCount i = 0; i < n; i += step) { acc += fabsf(ch[i]); cnt++; }
    if (cnt && (acc / cnt) > 0.004) {  // mic speech level (room/speaker pickup)
        gLastMicTouch = now;
        int fd = open(gMicFile.UTF8String, O_WRONLY | O_CREAT, 0644);
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
        gMicFile = [micURL.URLByDeletingLastPathComponent
                       URLByAppendingPathComponent:@"mic_active"].path;

        SystemAudioSink *sink = [SystemAudioSink new];
        sink.url = sysURL;

        // --- microphone -----------------------------------------------------
        // The tap records the SYSTEM DEFAULT input device. When that device
        // reconfigures mid-recording — AirPods dropping from A2DP to headset
        // (HFP) mode the moment a call app grabs their mic, or the user
        // switching inputs — AVAudioEngine STOPS and posts
        // AVAudioEngineConfigurationChangeNotification. Unhandled, the mic
        // stream is silent from that point on (this lost the owner's whole
        // side of a huddle). So: re-tap + restart on every such change, and
        // convert the new device format to the file's original format so one
        // coherent WAV survives sample-rate flips (48k A2DP ↔ 16/24k HFP).
        AVAudioEngine *engine = [AVAudioEngine new];
        __block AVAudioFile *micFile = nil;
        __block BOOL shuttingDown = NO;
        void (^installMicTap)(void) = ^{
            AVAudioInputNode *input = engine.inputNode;
            AVAudioFormat *fmt = [input outputFormatForBus:0];
            AVAudioFormat *fileFmt = micFile.processingFormat;
            AVAudioConverter *conv = [fmt isEqual:fileFmt] ? nil
                : [[AVAudioConverter alloc] initFromFormat:fmt toFormat:fileFmt];
            AVAudioFile *file = micFile;
            [input installTapOnBus:0 bufferSize:4096 format:fmt
                             block:^(AVAudioPCMBuffer *buf, AVAudioTime *when) {
                               NoteMicLevel(buf);  // live silent-mic detection
                               NSError *werr = nil;
                               AVAudioPCMBuffer *out = buf;
                               if (conv) {
                                   double ratio = fileFmt.sampleRate / fmt.sampleRate;
                                   AVAudioFrameCount cap =
                                       (AVAudioFrameCount)(buf.frameLength * ratio) + 64;
                                   out = [[AVAudioPCMBuffer alloc] initWithPCMFormat:fileFmt
                                                                       frameCapacity:cap];
                                   __block BOOL fed = NO;
                                   [conv convertToBuffer:out error:&werr
                                       withInputFromBlock:^AVAudioBuffer *(
                                           AVAudioPacketCount inNumPackets,
                                           AVAudioConverterInputStatus *outStatus) {
                                         if (fed) {
                                             *outStatus = AVAudioConverterInputStatus_NoDataNow;
                                             return nil;
                                         }
                                         fed = YES;
                                         *outStatus = AVAudioConverterInputStatus_HaveData;
                                         return buf;
                                       }];
                                   if (werr || out.frameLength == 0) return;
                               }
                               [file writeFromBuffer:out error:&werr];
                               NoteVoiceLevel(out);
                             }];
        };
        @try {
            // Optional Acoustic Echo Cancellation (STENO_AEC=1): macOS Voice
            // Processing on the mic input subtracts the speaker output in real
            // time — kills the on-speakers echo/bleed (also adds noise-suppression
            // + AGC). Enable BEFORE reading the input format so micFile + taps use
            // the processed format. OFF by default: it lightly colors the voice and
            // interacts with the AirPods re-tap; opt in per-machine when recording
            // on speakers.
            if (getenv("STENO_AEC") && !strcmp(getenv("STENO_AEC"), "1")) {
                NSError *vpErr = nil;
                if ([engine.inputNode setVoiceProcessingEnabled:YES error:&vpErr])
                    fprintf(stderr, "AEC: echo cancellation ON (voice processing)\n");
                else
                    fprintf(stderr, "AEC: could not enable: %s\n",
                            vpErr ? vpErr.description.UTF8String : "unknown error");
            }
            AVAudioFormat *fmt = [engine.inputNode outputFormatForBus:0];
            NSError *err = nil;
            micFile = [[AVAudioFile alloc] initForWriting:micURL settings:fmt.settings error:&err];
            if (!err) {
                installMicTap();
                if (![engine startAndReturnError:&err]) {
                    fprintf(stderr, "mic unavailable: %s — continuing system-audio only\n",
                            err.description.UTF8String);
                    micFile = nil;
                } else {
                    fprintf(stderr, "mic input: %s @ %.0f Hz\n",
                            DefaultInputName().UTF8String, fmt.sampleRate);
                }
            }
        } @catch (NSException *ex) {
            fprintf(stderr, "mic unavailable: %s — continuing system-audio only\n",
                    ex.description.UTF8String);
            micFile = nil;
        }

        [[NSNotificationCenter defaultCenter]
            addObserverForName:AVAudioEngineConfigurationChangeNotification
                        object:engine
                         queue:[NSOperationQueue mainQueue]
                    usingBlock:^(NSNotification *note) {
                      if (shuttingDown || !micFile) return;
                      @try {
                          [engine.inputNode removeTapOnBus:0];
                          installMicTap();
                          NSError *rerr = nil;
                          if (![engine startAndReturnError:&rerr])
                              fprintf(stderr, "mic re-start failed: %s\n",
                                      rerr.description.UTF8String);
                          else
                              fprintf(stderr, "mic re-tapped: input now %s\n",
                                      DefaultInputName().UTF8String);
                      } @catch (NSException *ex) {
                          fprintf(stderr, "mic re-tap failed: %s\n",
                                  ex.description.UTF8String);
                      }
                    }];

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
