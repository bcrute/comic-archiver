# Comic Archiver

> **Tapas closes on 31 March 2027.** Tapas says what you've unlocked there cannot be moved to another app, and
> purchases are not refunded. If you've paid for comics or novels on Tapas, this app saves them to your own
> computer before they're gone.

Comic Archiver saves the comics and novels you can read on Tapas, Tappytoon and Webtoon to your own computer, so you have
your own copy of what you've bought and unlocked.

You sign in to the site inside the app, press **Start**, and it works through your library. You can stop it whenever
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
There's a version for Windows 10 and 11, one for Linux and a first one for Macs with an Intel processor. The
Releases page explains how to install it, including the warnings Windows and macOS show because the app isn't
signed by a recognized publisher yet.

The Mac version is new: it is tested on macOS 15 on an Intel processor, and Apple has not notarized it. Build
notes are in [the macOS development instructions](docs/development.md#build-the-macos-package).

## Using it

1. On the dashboard, each site has its own card. Click **Sign in** on your site's card, and sign in with your email
   and password.
2. Press **Start** on that card. If you use more than one site, sign in to each: they all download at once, and
   **Start all** starts every one.

If you normally sign in with Google or Facebook, those buttons may not work inside the app. Set a password on your
account first (sign in the usual way in your web browser and look in the site's account
settings), then sign in to the app with your email and that password. Copying and pasting from a password manager
works; browser extensions don't run inside the app.

On Tappytoon it saves free chapters and the ones you've bought or are renting. A rented chapter is labeled with
the date the rental ends.

On Webtoon it checks your subscriptions and your account's Unlocked list. It saves
website-readable episodes and account-list candidates when the authenticated reader
returns complete content, including app-only episodes. Pricing, ownership and expiry
flags guide discovery; they do not override a successful reader response. Actual
provider refusals are respected. Separate whole-series purchases and exhaustive
account completeness have not yet been qualified.

Downloads and storage cards show Free, Bought / unlocked, Limited and Unknown type
episodes, with separate saved and pending counts. These are recorded classifications;
unknown types remain unclassified. The provider's reader establishes access; catalogue flags alone do not reject a complete reader response.

Use **Download: All types** on a provider's dashboard card to choose Paid / unlocked,
Free, Limited unlocks, and Unknown type episodes. All types are initially selected.
Paid / unlocked also includes free episodes from series with a recorded permanent
purchase or unlock. Uncheck Free to leave unrelated free-only series out of future
downloads, including restoration of deleted episodes. Choices are saved separately
for each provider and never delete existing files. A running download pauses to
apply a change, then continues; a paused download stays paused. Limited unlocks are
existing grants only: the app never starts WUF timers, claims episodes, or buys
anything. Unknown type keeps episodes whose provider labels are inconclusive
eligible for the reader to check; deselecting it excludes those unknown types.
Unknown type can include free episodes when the provider cannot identify their
type, so deselect that category as well for confirmed paid series only.

The Library page expands from provider to series to recorded episodes, with a
saved/pending indicator for each episode. Format shows CBZ/EPUB (or the recorded
legacy format); Access shows free/unlocked/limited classifications and marks
mixed series. Unknown values stay explicit. It reads the local inventory on demand;
it does not browse files or query providers. Saved can include cloud storage, and
an unlisted episode may simply not have been discovered yet. Refresh updates the
indicators. Episode selection and selective downloading are not enabled yet.

If you'd like a copy in the cloud as well, the app can also save to your own Backblaze B2 storage. Settings walks
you through setting it up.

To update, install the new version over the old one; don't uninstall first. On Windows, run the new setup file. On
Linux, close Comic Archiver, then unpack the new version and start it; it offers to delete the older copy. Either
way you stay signed in, and your settings and saved episodes stay.

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
| macOS (Intel) | First version; tested on macOS 15, not notarized |
| macOS (Apple silicon) | Not available yet |

## Other ways to save your comics

Since Tapas announced its closing, community guides often point to Mihon, HaruNeko and Suwayomi. Those are good
tools, and if you already read in one of them, keep using it. Comic Archiver is different:

- It saves **novels as well as comics**. Novels come out as EPUB files that open in any e-book reader. If you've
  bought novels on Tapas, check that the tool you choose saves them; not all do.
- It's one program you install and press Start. No extension repositories to add, no phone, no Docker.
- It saves what your account has bought or unlocked on Tapas, Tappytoon and Webtoon, and picks up where it left
  off.

Tapas discovery includes the account Library’s Wait Until Free list as well as Free Episodes.
These lists supply series to check; appearing there does not establish episode access or
activate an unlock. The app still uses readable episode candidates and the authenticated
reader response. It does not claim tickets, spend currency, or activate waiting periods.
If a listed series’ header returns 404 or 410, its observed numeric identity and title allow
the app to check the episode list and reader. Authentication failures, throttling, invalid
identities, incomplete content, and other metadata errors retain their existing checks.

## Building it yourself

Notes for building the app, checking a change and making a release are in
[docs/development.md](docs/development.md).

## How it's tested

More than 2,000 automated tests check the app. Before every release, a separate check deliberately breaks the code
in more than 500 places and makes sure the tests notice each one. The app is then built, installed, started and
uninstalled on Windows 11, and built and started on Linux. A release goes out only when all of that passes.

Comic Archiver is written with the help of AI coding tools, and every change goes through those same checks.

## License

MIT; see [LICENSE](LICENSE). Bundled components are listed in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
