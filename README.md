[Download for Windows and Linux](https://github.com/bcrute/comic-archiver/releases/latest).

# Comic Archiver

> **Tapas closes on 31 March 2027.** Tapas says what you've unlocked there cannot be moved to another app, and
> purchases are not refunded. If you've paid for comics or novels on Tapas, this app saves them to your own
> computer before they're gone.

Comic Archiver saves the comics and novels you can read on Tapas, Tappytoon and Webtoon to your own computer, so you have
your own copy of what you've bought and unlocked.

You sign in to the site inside the app, press **Go**, and it works through your library. You can stop it whenever
you like; next time it picks up where it left off.

Comics are saved as CBZ files and novels as EPUB files, which open in most comic and e-book reader apps. Everything
goes into a folder called `Comic Archiver` in your user folder, one folder per series.

## Your privacy

**Comic Archiver doesn't track you, and your login only ever goes to the comic site.**

- **No tracking of any kind.** No analytics, no usage statistics, no crash reports, no ads. The app never contacts
  me or any server of mine. There isn't one.
- **Your password goes straight to the comic site.** When you sign in, you're typing into the comic site's
  own sign-in page, shown inside the app's window. Your password goes to the site, just as it would in your web
  browser. The app never reads it and never saves it.
- **What the app keeps, and where.** Once you're signed in, the site hands back a sign-in cookie, the same thing
  that keeps you logged in in a web browser. The app keeps it on your computer so it can download your episodes,
  and only ever sends it back to that same site. Signing out in **Settings → Downloads** removes it from your
  computer, and uninstalling removes every one. That signs out only the app: your account on the site, and
  anywhere else you're signed in, aren't affected.
- **Who the app talks to.** The comic site you picked, and the servers it keeps its images on. That's it. If you
  choose to back up to your own Backblaze B2 cloud storage, the app also talks to Backblaze, using the key you give
  it, and downloads a small helper program (rclone) from its official page once, checking that it's the genuine file.

The sign-in page is the comic site's real page, so it behaves the way that site normally does in your browser.

You don't have to take my word for any of this: all of the app's code is here for anyone to read.

## Getting it

Download the newest version from the [Releases page](https://github.com/bcrute/comic-archiver/releases/latest).
There's a version for Windows 10 and 11 and one for Linux. The Releases page explains how to install it, including
the warnings Windows shows because the app isn't signed yet.

## Using it

1. Pick your site in the **Site** list.
2. Click **Sign in**, and sign in with your email and password.
3. Press **Go**.

If you normally sign in with Google or Facebook, those buttons may not work inside the app. Set a password on your
account first (sign in the usual way in your web browser and look in the site's account
settings), then sign in to the app with your email and that password. Copying and pasting from a password manager
works; browser extensions don't run inside the app.

On Tappytoon it saves free chapters and the ones you've bought or are renting. A rented chapter is labelled with
the date the rental ends.

On Webtoon it saves the series you subscribe to, every episode the website shows. Episodes Webtoon only lets you
read in its phone app, such as Daily Pass and Fast Pass ones, aren't on the website, so they aren't saved.

If you'd like a copy in the cloud as well, the app can also save to your own Backblaze B2 storage. Settings walks
you through setting it up.

Uninstalling signs the app out of every site and removes your Backblaze key from this computer. Your saved episodes
stay. On Windows, uninstall it from **Settings → Apps** as usual. On Linux, right-click Comic Archiver in your
applications menu, choose **Uninstall**, then delete the folder you unpacked it into.

## What works

| | |
|---|---|
| Tapas | Works |
| Tappytoon | Works |
| Webtoon | Works |
| Windows 10 and 11 | Works |
| Linux | Works |
| Mac | Not planned yet (no hardware) |
| Android | By Nov 1 |

## Other ways to save your comics

Since Tapas announced its closing, community guides often point to Mihon, HaruNeko and Suwayomi. Those are good
tools, and if you already read in one of them, keep using it. Comic Archiver is different:

- It saves **novels as well as comics**. Novels come out as EPUB files that open in any e-book reader. If you've
  bought novels on Tapas, check that the tool you choose saves them; not all do.
- It's one program you install and press Go. No extension repositories to add, no phone, no Docker.
- It saves what your account has bought or unlocked on Tapas, Tappytoon and Webtoon, and picks up where it left
  off.

## Building it yourself

Notes for building the app, checking a change and making a release are in
[docs/development.md](docs/development.md).

## Who made this

I’m a software engineer with more than 15 years of experience. I design and maintain Comic Archiver, and I use AI tools to help with development.

The project has more than 1,600 automated tests and roughly 97% code coverage. Before each release, automated checks build and start the app on Windows 11 and Linux. The Windows checks also cover installation and uninstallation.

## License

MIT; see [LICENSE](LICENSE). Bundled components are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
