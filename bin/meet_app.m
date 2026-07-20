// meet_app.m — native macOS window for the meeting-intelligence UI.
//
// A ~120-line WKWebView shell around http://127.0.0.1:8788 (meet_ui.py):
// real desktop app (dock icon, own window, Cmd-Q) with none of the Electron
// weight. On launch it starts meet_ui.py if the port is closed; the server is
// shared with any browser tab and stays up after the app quits.
//
// Build (REPO_PATH baked in at compile time so the tracked source stays
// machine-agnostic):
//   clang -fobjc-arc -O2 -DREPO_PATH='"/path/to/repo"' bin/meet_app.m \
//     -o "Meet.app/Contents/MacOS/Meet" -framework Cocoa -framework WebKit

#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>
#include <arpa/inet.h>

#ifndef REPO_PATH
#error "compile with -DREPO_PATH='\"/abs/path/to/repo\"'"
#endif

static const int kPort = 8788;

static BOOL portOpen(void) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return NO;
    struct sockaddr_in a = {0};
    a.sin_family = AF_INET;
    a.sin_port = htons(kPort);
    inet_pton(AF_INET, "127.0.0.1", &a.sin_addr);
    struct timeval tv = {0, 300000};
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    BOOL ok = connect(s, (struct sockaddr *)&a, sizeof(a)) == 0;
    close(s);
    return ok;
}

@interface AppDelegate : NSObject <NSApplicationDelegate>
@property(strong) NSWindow *win;
@property(strong) WKWebView *web;
@end

@implementation AppDelegate
- (void)applicationDidFinishLaunching:(NSNotification *)note {
    if (!portOpen()) {
        NSTask *t = [NSTask new];
        t.executableURL = [NSURL fileURLWithPath:@"/usr/bin/env"];
        t.arguments = @[ @"python3", @REPO_PATH "/bin/meet_ui.py" ];
        t.standardOutput = nil;
        t.standardError = nil;
        [t launchAndReturnError:nil];
    }

    NSRect frame = NSMakeRect(0, 0, 1180, 760);
    self.win = [[NSWindow alloc]
        initWithContentRect:frame
                  styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                            NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable
                    backing:NSBackingStoreBuffered
                      defer:NO];
    self.win.title = @"Steno";
    self.win.minSize = NSMakeSize(760, 480);
    [self.win center];

    WKWebViewConfiguration *cfg = [WKWebViewConfiguration new];
    self.web = [[WKWebView alloc] initWithFrame:frame configuration:cfg];
    self.web.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
    self.win.contentView = self.web;

    // Standard edit menu so copy/paste works in the scratchpad.
    NSMenu *bar = [NSMenu new];
    NSMenuItem *appItem = [NSMenuItem new];
    NSMenu *appMenu = [NSMenu new];
    [appMenu addItemWithTitle:@"Quit Steno" action:@selector(terminate:) keyEquivalent:@"q"];
    appItem.submenu = appMenu;
    [bar addItem:appItem];
    NSMenuItem *editItem = [NSMenuItem new];
    NSMenu *edit = [[NSMenu alloc] initWithTitle:@"Edit"];
    [edit addItemWithTitle:@"Cut" action:@selector(cut:) keyEquivalent:@"x"];
    [edit addItemWithTitle:@"Copy" action:@selector(copy:) keyEquivalent:@"c"];
    [edit addItemWithTitle:@"Paste" action:@selector(paste:) keyEquivalent:@"v"];
    [edit addItemWithTitle:@"Select All" action:@selector(selectAll:) keyEquivalent:@"a"];
    editItem.submenu = edit;
    [bar addItem:editItem];
    // View menu — Cmd-R reload so UI updates load without quitting the app
    // (a WKWebView doesn't refresh on its own when meet_ui.py changes).
    NSMenuItem *viewItem = [NSMenuItem new];
    NSMenu *viewMenu = [[NSMenu alloc] initWithTitle:@"View"];
    [viewMenu addItemWithTitle:@"Reload" action:@selector(reloadWeb:) keyEquivalent:@"r"];
    viewItem.submenu = viewMenu;
    [bar addItem:viewItem];
    NSApp.mainMenu = bar;

    [self loadWhenReady:0];
    [self.win makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
}

- (void)loadWhenReady:(int)attempt {
    if (portOpen()) {
        NSURL *u = [NSURL URLWithString:
            [NSString stringWithFormat:@"http://127.0.0.1:%d/", kPort]];
        [self.web loadRequest:[NSURLRequest requestWithURL:u]];
        return;
    }
    if (attempt > 20) {
        [self.web loadHTMLString:@"<h2 style='font-family:sans-serif;padding:40px'>"
                                  "meet_ui.py did not come up on 127.0.0.1:8788</h2>"
                         baseURL:nil];
        return;
    }
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.5 * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{ [self loadWhenReady:attempt + 1]; });
}

- (void)reloadWeb:(id)sender {
    // Reload from the server (not cache) so edited JS/CSS actually takes.
    [self.web loadRequest:[NSURLRequest
        requestWithURL:self.web.URL
           cachePolicy:NSURLRequestReloadIgnoringLocalCacheData
       timeoutInterval:15]];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)app {
    return YES;
}
@end

int main(void) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        app.activationPolicy = NSApplicationActivationPolicyRegular;
        AppDelegate *d = [AppDelegate new];
        app.delegate = d;
        [app run];
    }
    return 0;
}
