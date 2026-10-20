# Comic Archiver

Comic Archiver saves the comics and novels you can read on Tapas and Tappytoon to your own computer, so you have
your own copy of what you've bought and unlocked.

You sign in to the site inside the app, press **Go**, and it works through your library. You can stop it whenever
you like; next time it picks up where it left off.

Comics are saved as CBZ files and novels as EPUB files, which open in most comic and e-book reader apps. Everything
goes into a folder called `Comic Archiver` in your user folder, one folder per series.

## Your privacy

**Comic Archiver doesn't track you, and your login only ever goes to the comic site.**

- **No tracking of any kind.** No analytics, no usage statistics, no crash reports, no ads. The app never contacts
  me or any server of mine. There isn't one.
- **Your password goes straight to the comic site.** When you sign in, you're typing into Tapas's or Tappytoon's
  own sign-in page, shown inside the app's window. Your password goes to the site, just as it would in your web
  browser. The app never reads it and never saves it.
- **What the app keeps, and where.** Once you're signed in, the site hands back a sign-in cookie, the same thing
  that keeps you logged in in a web browser. The app keeps it on your computer so it can download your episodes,
  and only ever sends it back to that same site. Signing out in **Settings → Downloads** removes it from your
  computer. That signs out only the app: your account on the site, and anywhere else you're signed in, aren't
  affected.
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
Tapas or Tappytoon account first (sign in the usual way in your web browser and look in the site's account
settings), then sign in to the app with your email and that password. Copying and pasting from a password manager
works; browser extensions don't run inside the app.

On Tappytoon it saves free chapters and the ones you've bought or are renting. A rented chapter is labelled with
the date the rental ends.

If you'd like a copy in the cloud as well, the app can also save to your own Backblaze B2 storage. Settings walks
you through setting it up.

Uninstalling removes only the app. Your saved episodes and your sign-in stay, so sign out first if you'd like the
sign-in gone too.

## What works

| | |
|---|---|
| Tapas | Works |
| Tappytoon | Works |
| Webtoon | Coming later |
| Windows 10 and 11 | Works |
| Linux | Works |
| Mac | Not planned yet |

## Building it yourself

Notes for building the app, checking a change and making a release are in
[docs/development.md](docs/development.md).

## Licence

MIT; see [LICENSE](LICENSE). Bundled components are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
