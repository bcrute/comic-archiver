## Changes in {version}

- **Nothing downloads until you press Go.** Opening Comic Archiver could carry on saving episodes from an earlier
  download by itself, without Go. It now waits for **Go**, and Go picks up where the download left off.
- **The window shows a download that is still saving.** When a download stopped on a problem, episodes could keep
  saving while the window showed it as stopped. The window now shows the download as working until it has nothing
  left to save, and **Pause** stops it.
- **Pause stops a download left by an older version.** Before, Pause could fail with "No account export is
  configured" and the download went on.

## Download

| Your computer | File |
|---|---|
| Windows 10 or 11 (64-bit) | `ComicArchiver-{version}-windows-x86_64-setup.exe` |
| Windows, without installing | `ComicArchiver-{version}-windows-x86_64.zip` |
| Linux (x86_64) | `ComicArchiver-{version}-linux-x86_64.tar.gz` |

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

Uninstalling signs Comic Archiver out of every site and removes your Backblaze key from this computer; your saved
episodes are kept.

**Linux.** Unpack the file, open the folder and double-click `comic-archiver` (choose "Run" if asked). After the
first start it is in your applications menu. Files go to `Comic Archiver` in your home folder, as above. To
uninstall, right-click Comic Archiver in the applications menu, choose **Uninstall**, then delete the folder.

`SHA256SUMS.txt` lists each file's SHA-256, to check a download against. These files were built and self-tested
from the tagged source. `release-evidence.json` records the validation results and download
hashes checked by the Release workflow before publication.
