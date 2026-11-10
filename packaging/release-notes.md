| Your computer | File |
|---|---|
| Windows 10 or 11 (64-bit) | [`ComicArchiver-{version}-windows-x86_64-setup.exe`](https://github.com/bcrute/comic-archiver/releases/download/v{version}/ComicArchiver-{version}-windows-x86_64-setup.exe) |
| Windows, without installing | [`ComicArchiver-{version}-windows-x86_64.zip`](https://github.com/bcrute/comic-archiver/releases/download/v{version}/ComicArchiver-{version}-windows-x86_64.zip) |
| Linux (x86_64) | [`ComicArchiver-{version}-linux-x86_64.tar.gz`](https://github.com/bcrute/comic-archiver/releases/download/v{version}/ComicArchiver-{version}-linux-x86_64.tar.gz) |
| Mac (Intel) | [`ComicArchiver-{version}-macos-x86_64.zip`](https://github.com/bcrute/comic-archiver/releases/download/v{version}/ComicArchiver-{version}-macos-x86_64.zip) |

## Changes in {version}

- **Choose which episode types to save.** Each provider offers paid/unlocked, free, limited-unlock and unknown-type choices. Paid series include their free companion episodes; existing saved files are kept.
- **Check your episode list.** The read-only Library view groups series and episodes by provider, so you can see which episodes have been saved.
- **More readable novel chapters can be saved.** EPUB export preserves supported legacy font markup and tables, with clearer diagnostics for unsupported content.

- **Every site downloads at once.** Signed in to more than one of Tapas, Tappytoon and Webtoon? Each one now
  downloads alongside the others. **Start all** and **Pause all** start and pause them together.
- **A dashboard you can read at a glance.** One card across the top counts everything saved from every site, beside
  where it is kept. Then each site has a card of its own, with what it is doing in a few short lines and one button
  for what can be done now: **Sign in**, **Start**, **Continue** or **Pause**. Time and space left each end in a
  **?** that says they are rough guesses. The cards reflow to fit the window.
- **A download goes on by itself after a wait.** Comic Archiver now keeps one record of your download instead of
  three that could disagree, which is what made a download pause for no clear reason. A dropped connection is
  tried again after 1, 2, 4, 8 and 15 minutes, then every 15 minutes until it is back. A step that saved nothing,
  or a site that refuses, waits 5, 15 and then 60 minutes before the download stops and asks you.
- **Episodes the app can't save yet no longer stop a download.** Before, a download that met episodes in a form the
  app can't save yet ended by saying they could not be saved and asking you to press Go, which never helped. It
  now finishes, and its card says how many there are. The download and storage details list them by reason.
- **More control over download speed.** Medium aims for about half the rate of Fast, and Slow for about a quarter.
- **When a download stops, the window says why.** Each stop comes with what to do about it, such as signing in
  again. Press **Continue** once you have.
- **Pause stops the download, and Continue carries on from there.** Closing the window or switching off the computer
  pauses it the same way.
- **Your download carries on after the update.** The first start moves it to the new record, and **Continue** picks up
  where it stopped. Keep using this version after the update: older versions do not understand the new
  library folder layout.
- **Asking for help is easier.** **Settings → About → Copy details for support** copies a short summary of each
  site's download to paste into a message. It has no series titles, folder names or sign-in details. If you are
  asked for the app's log, **Save log for support** next to it puts everything the app recorded in one file in
  your Downloads folder, to attach to an email. Series and episodes are in it by number only, never by title, and
  it has no folder names or sign-in details.

- **Existing libraries are organized automatically.** On startup, the app moves older Tapas folders into
  `Tapas` beside `Tappytoon` and `Webtoon` in your chosen library folder. Saved archives are moved, not copied.
  **Open library folder** opens that folder in your usual file manager.
- **Clearer progress with less background work.** Local file checks show their progress. Estimates say when
  downloads have not been queued yet, and paused sites stop refreshing. Dashboard fields stay in place.
- **More reliable image recovery.** Valid images with extra trailing data are preserved, and episodes blocked
  by the older image check are retried automatically. Support logs include the details needed to diagnose
  image failures.

- **Private sign-ins in copied Windows libraries.** Existing app state receives the same restricted access
  as new state when opened. Saved archive permissions stay as they are. The library drive must support
  Windows file access permissions, such as NTFS. A Windows path-length failure while recording cloud
  backups is also fixed.

Each is self-contained: nothing else needs installing.

<details>
<summary>Windows install and update</summary>

The app is not signed yet, so Windows warns about it twice. Both warnings are expected.

1. If Edge says the file "isn't commonly downloaded", point at the download, choose **…**, then **Keep**. If it
   asks again, choose **Show more**, then **Keep anyway**.
2. Run the setup file. If Windows says "Windows protected your PC", its only button is **Don't run**. Don't
   press it. Click the small **More info** link under the message instead:

   ![The "Windows protected your PC" window, with the small More info link under the message circled](https://raw.githubusercontent.com/bcrute/comic-archiver/v{version}/docs/images/windows-warning-1-more-info.png)

3. A **Run anyway** button then appears beside **Don't run**. Click **Run anyway**:

   ![The same window after More info, with the Run anyway button circled](https://raw.githubusercontent.com/bcrute/comic-archiver/v{version}/docs/images/windows-warning-2-run-anyway.png)

   It installs Comic Archiver for your own account and never asks for an administrator.

If Windows says Smart App Control blocked it and there is no **Run anyway**, Comic Archiver can't be installed on
that PC yet.

The installer's window and the app's first start can each take up to a minute while Windows checks the files;
later starts are quick.

**The first time**

1. On the dashboard, each site has its own card. Choose **Sign in** on your site's card, and sign in on the site's
   own page in the window with your email and password.
   Google and Facebook sign-in buttons may not work in that window. If that is how you normally sign in, first add
   a password to your account on the comic site itself (Tapas, Tappytoon or Webtoon, not Google or Facebook): sign in to the
   comic site in your usual web browser the way you always do, open your account settings, and set a password
   there. Then sign in to Comic Archiver with your account's email and that password.
2. Press **Start** on that card. Signed in to more than one site? **Start all** starts every one.

**Where your files go.** Into the folder `Comic Archiver` in your user folder (on Windows
`C:\Users\<your name>\Comic Archiver`): Tapas in `Tapas\Library`, Tappytoon in `Tappytoon\Library`, Webtoon in `Webtoon\Library`, one folder per series.
Comics are saved as CBZ files and novels as EPUB files, which open in comic and e-book reader apps.

**Updating.** Run the new setup file over the old version; don't uninstall first. If Comic Archiver is open, setup
closes it first, and a download pauses until you press Continue; version 0.2.3 or older asks you to close it yourself. Your saved sign-in data, settings and episodes stay. A provider can still require a new sign-in when its session expires. Uninstalling signs Comic Archiver out of every site and
removes your Backblaze key from this computer; your saved episodes are kept.

</details>

<details>
<summary>Linux install and update</summary>

Unpack the file, open the folder and double-click `comic-archiver` (choose "Run" if asked). After the
first start it is in your applications menu. Files go to `Comic Archiver` in your home folder, as above. To
update, close Comic Archiver, unpack the new version and start it: it offers to delete the older copy, and you
keep your saved sign-in data and settings. A provider can still require a new sign-in when its session expires. To
uninstall, right-click Comic Archiver in the applications menu, choose **Uninstall**, then delete the folder.

</details>

<details>
<summary>Mac install and update</summary>

Download and extract the ZIP, then move `Comic Archiver.app` to Applications.
This build includes Python and Qt. Native testing covers Intel macOS 15;
Apple Silicon execution remains unqualified.

The app is ad-hoc signed and is not notarized by Apple. If macOS blocks it as
an unidentified developer, review the download and follow
[Apple's instructions for opening an unnotarized app](https://support.apple.com/en-gb/102445).

To update, close Comic Archiver and replace the app. Keep your library and settings.
The included `README_FIRST.txt` describes removal and saved-file preservation.

</details>

`SHA256SUMS.txt` lists each file's SHA-256, to check a download against. These files were built and self-tested
from the tagged source. `release-evidence.json` records the validation results and download
hashes checked by the Release workflow before publication.
