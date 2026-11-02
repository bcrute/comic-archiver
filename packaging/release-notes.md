## Changes in {version}

- **Every site downloads at once.** Signed in to more than one of Tapas, Tappytoon and Webtoon? Each one now
  downloads alongside the others. **Go on all sites** and **Pause all** start and pause them together.
- **A dashboard you can read at a glance.** One card counts everything saved from every site. Then each site has a
  card of its own, with what it is doing in a few short lines and its own **Go**, **Pause** and **Sign in**. A wide
  window shows two cards to a row.
- **A download goes on by itself after a wait.** Comic Archiver now keeps one record of your download instead of
  three that could disagree, which is what made a download pause for no clear reason. A dropped connection is
  tried again after 1, 2, 4, 8 and 15 minutes, then every 15 minutes until it is back. A step that saved nothing,
  or a site that refuses, waits 5, 15 and then 60 minutes before the download stops and asks you.
- **When a download stops, the window says why.** Each stop comes with what to do about it, such as signing in
  again. Press **Go** once you have.
- **Pause stops the download, and Go carries on from there.** Closing the window or switching off the computer
  pauses it the same way.
- **Your download carries on after the update.** The first start moves it to the new record, and **Go** picks up
  where it stopped. Going back to an older version after that starts the list and series checks again; nothing
  you have saved is lost.

## Download

| Your computer | File |
|---|---|
| Windows 10 or 11 (64-bit) | [`ComicArchiver-{version}-windows-x86_64-setup.exe`](https://github.com/bcrute/comic-archiver/releases/download/v{version}/ComicArchiver-{version}-windows-x86_64-setup.exe) |
| Windows, without installing | [`ComicArchiver-{version}-windows-x86_64.zip`](https://github.com/bcrute/comic-archiver/releases/download/v{version}/ComicArchiver-{version}-windows-x86_64.zip) |
| Linux (x86_64) | [`ComicArchiver-{version}-linux-x86_64.tar.gz`](https://github.com/bcrute/comic-archiver/releases/download/v{version}/ComicArchiver-{version}-linux-x86_64.tar.gz) |

Each is self-contained: nothing else needs installing.

**Windows.** The app is not signed yet, so Windows warns about it twice. Both warnings are expected.

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
2. Press **Go** on that card. Signed in to more than one site? **Go on all sites** starts every one.

**Where your files go.** Into the folder `Comic Archiver` in your user folder (on Windows
`C:\Users\<your name>\Comic Archiver`): Tapas in `Library`, Tappytoon in `Tappytoon\Library`, Webtoon in `Webtoon\Library`, one folder per series.
Comics are saved as CBZ files and novels as EPUB files, which open in comic and e-book reader apps.

**Updating.** Run the new setup file over the old version; don't uninstall first. If Comic Archiver is open, setup
closes it first, and a download pauses until you press Go; version 0.2.3 or older asks you to close it yourself. You
stay signed in, and your settings and saved episodes stay. Uninstalling signs Comic Archiver out of every site and
removes your Backblaze key from this computer; your saved episodes are kept.

**Linux.** Unpack the file, open the folder and double-click `comic-archiver` (choose "Run" if asked). After the
first start it is in your applications menu. Files go to `Comic Archiver` in your home folder, as above. To
update, close Comic Archiver, unpack the new version and start it: it offers to delete the older copy, and you
stay signed in. To
uninstall, right-click Comic Archiver in the applications menu, choose **Uninstall**, then delete the folder.

`SHA256SUMS.txt` lists each file's SHA-256, to check a download against. These files were built and self-tested
from the tagged source. `release-evidence.json` records the validation results and download
hashes checked by the Release workflow before publication.
