# An Android app: what it would take

Scoping notes from 21 October 2026. Nothing here is started; this says what a first
Android version would be made of, what could stop it, and what it would cost. It ends
with the decision it is asking for.

## Why

Tapas closes on 31 March 2027, and most of its readers read on a phone. On Android the
known tools (Mihon and its extensions) save comics into a reader app; nothing saves
purchased novels as files. A phone version reaches the readers the desktop version
cannot.

## What carries over unchanged

The app is already split the way a port needs. Of about 20,100 lines in
`comic_archive/`, only two files know about Qt: `qt_engine.py` (the web engine behind
sign-in) and `desktop_app.py` (the window). Everything else — the site adapters, the
saving worker, the archive, jobs, inventory, novels, publication — is plain Python that
needs no Qt.

The sign-in store is already engine-agnostic: `session.py` takes whatever a browser
gives it, keeps only the site's own cookies and storage, and writes them privately.
The engine is a seam, and the desktop already has two implementations of it (QtWebEngine
and Playwright).

The core's dependencies are `httpx` (pure Python) and `zstandard` (native code, used by
`publication.py` to decode compressed episode data). `tzdata` is Windows-only and does
not apply.

## What an Android version is

A native Android app (Kotlin and Compose) that embeds the Python core with
[Chaquopy](https://chaquo.com/chaquopy/). Not PySide6 on Android: Qt now ships Android
wheels, but its phone packages do not include QtWebEngine, and the desktop's window is
wrong for a phone's screen anyway. A native shell keeps sign-in idiomatic (the system
WebView) and the app small. Chaquopy wants Android Gradle Plugin 7.3–9.4 and a minimum
Android SDK of 24, offers Python 3.11 among its versions, and needs no NDK for its
prebuilt packages.

Three pieces:

1. **The window.** Site list, sign-in, Go and Pause, progress, the library. One screen
   at a time, the same plain words as the desktop.
2. **The engine.** Android's WebView and CookieManager standing in for `qt_engine.py`:
   load the site's real sign-in page, watch cookies arrive the way `CookieRecords`
   does, restore saved storage, and know when sign-in has settled. `session.py` keeps
   working unchanged behind it.
3. **The core.** `comic_archive` as it is, run by the worker the same way the desktop
   runs it, saving into the app's own folder on the device. A foreground service with
   a notification keeps a long download alive; Pause works as on the desktop.

## What could stop it (the spikes, in order)

1. **zstandard on Chaquopy.** Chaquopy prebuilds many native packages; whether
   zstandard is among them is unconfirmed. If not, the wheel can be built once with
   the NDK, or the decode moved behind a small native shim. Also confirm Chaquopy's
   licence terms for an open-source app. A day to know both.
2. **The WebView engine against the stand-in site.** The engine smoke check already
   runs the real engine against a local stand-in site; the WebView engine must pass
   the same shape of check. A few days.
3. **Worker wiring from Kotlin.** Starting, pausing and reporting across the Chaquopy
   boundary; mostly plumbing. A few days.
4. **A signed APK from the Linux build machine.** Gradle and the Android SDK on the
   CI hosts, the keystore kept the way release credentials are kept.

## What a first version leaves out

Cloud saving (Backblaze), the mobile-data choice, choosing single series, and anything
for iPhone: Apple allows no sideloading, and its store carries the same refusal risk
as Google's. Mac stays where it is.

## Distribution

The APK joins the GitHub releases beside the Windows and Linux downloads. No store:
Google's rules have already removed download tools, and the reader community sideloads
anyway (Mihon is installed the same way). Listing with Obtainium and IzzyOnDroid gives
updates without a store. The signing key is made once and kept like the release
credentials: a new key means every person using the app must install it again.

## Cost against 31 March 2027

The spikes take about a week. The window, sign-in and saving take two to three weeks,
with the owner testing each site's real sign-in at the app as on the desktop. Polish
and distribution take another week or two. A first usable APK in four to six focused
weeks, eight with slack. Starting in November leaves the whole of March as buffer.

That is the decision this page asks for: the Android app reaches the readers who are
losing their purchases; the desktop work it would displace (Backblaze saving, the
mobile-data choice) improves a copy those readers do not have yet.
