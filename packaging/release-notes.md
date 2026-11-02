## Changes in {version}

- **A download keeps going after an update.** After updating to 0.2.4, a download could pause at once, saying the
  last tries saved nothing new, because the update starts the series checks over. It now carries on. Series checked
  before the update are checked again first, so bought episodes the older version refused are saved sooner.
- **Long downloads no longer stop by themselves.** A download that had run for weeks could stop as if something
  had failed, and Go after a stop could pause again straight away. Both are fixed.
- **Settings › Downloads says why episodes were not downloaded.** It opens with a table of every reason, such as
  locked on your account or not released yet, and how many checked episodes each one kept back.
- **Renaming or moving the library folder while it is open is safe.** It used to split the library in two. Now
  the download stops and the window asks where the folder went; a folder renamed while the app was closed is asked
  for at the next start.
- **A sign-in that stops working during a download shows Sign-in needed,** even when the check at opening said it
  was fine.
- **Licenses.** Settings › About shows every license the app is built under. The Linux app is about 300 MB smaller.
- **Fixes.** The window says why the first save waits until every list is read. Settings shows the speed of the
  open site only. Text uses American spelling.

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

1. Choose your site in the Site list.
2. Choose **Sign in**, and sign in on the site's own page in the window with your email and password.
   Google and Facebook sign-in buttons may not work in that window. If that is how you normally sign in, first add
   a password to your account on the comic site itself (Tapas, Tappytoon or Webtoon, not Google or Facebook): sign in to the
   comic site in your usual web browser the way you always do, open your account settings, and set a password
   there. Then sign in to Comic Archiver with your account's email and that password.
3. Press **Go**.

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
