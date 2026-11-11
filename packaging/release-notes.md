| Your computer | File |
|---|---|
| Windows 10 or 11 (64-bit) | [`ComicArchiver-{version}-windows-x86_64-setup.exe`](https://github.com/bcrute/comic-archiver/releases/download/v{version}/ComicArchiver-{version}-windows-x86_64-setup.exe) |
| Windows, without installing | [`ComicArchiver-{version}-windows-x86_64.zip`](https://github.com/bcrute/comic-archiver/releases/download/v{version}/ComicArchiver-{version}-windows-x86_64.zip) |
| Linux (x86_64) | [`ComicArchiver-{version}-linux-x86_64.tar.gz`](https://github.com/bcrute/comic-archiver/releases/download/v{version}/ComicArchiver-{version}-linux-x86_64.tar.gz) |
| Mac with an Intel processor (macOS 13 or newer) | [`ComicArchiver-{version}-macos-x86_64.zip`](https://github.com/bcrute/comic-archiver/releases/download/v{version}/ComicArchiver-{version}-macos-x86_64.zip) — first Mac version: not notarized by Apple, and tested only on macOS 15 |

## Changes in {version}

- **A first version for Mac.** Comic Archiver now runs on Macs with an Intel processor. Apple has not
  notarized it, so macOS asks you to allow it the first time; the Mac section below shows how. Macs with Apple
  silicon are not supported yet.
- **Fewer downloads that stop partway.** A download no longer pauses because a site's own episode count
  disagrees with its episode list. An episode the site has removed or left empty is set aside so the rest can
  finish, and **Continue** tries the set-aside episodes again. When a site is down, the download waits and
  carries on without giving up on its episodes.
- **A clearer dashboard.** Provider cards are larger, with their status, estimates and transfer rate easier to
  read, and the window shows a change as soon as it has happened.
- **Libraries on more Windows drives.** A library can be kept on a drive that stores no access permissions,
  such as an exFAT or FAT32 external drive.
- **More novel chapters saved.** Chapters with tables whose cells span rows or columns are saved as EPUB.
- **Moving a library is safer.** A move that was interrupted carries on after a restart, and no sign-in or
  cloud key is left behind in a half-moved folder.
- **Lighter on your disk.** The app reads the library less often while it works, which matters most on hard
  disks and large libraries.

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

This first Mac version is for Macs with an Intel processor running macOS 13 or newer. It has been tested on
macOS 15 only. Apple has not notarized the app, so macOS will not open it until you allow it once.

1. Open the downloaded ZIP file, then drag `Comic Archiver` into your Applications folder.
2. Open Comic Archiver. macOS says it could not verify the app. Choose **Done**, not **Move to Trash**.
3. Open **System Settings**, choose **Privacy & Security** and scroll down to **Security**. Beside the message
   about Comic Archiver, choose **Open Anyway**, then confirm with your password or Touch ID.

After that it opens normally. Files go to `Comic Archiver` in your home folder, as above. To update, close Comic
Archiver and replace the app in Applications with the new one; you keep your saved sign-in data and settings, and
macOS asks you to allow the new version once more.

</details>



`SHA256SUMS.txt` lists each file's SHA-256, to check a download against. These files were built and self-tested
from the tagged source. `release-evidence.json` records the validation results and download
hashes checked by the Release workflow before publication.
