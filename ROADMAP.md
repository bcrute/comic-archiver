# Roadmap

What comes next for Comic Archiver, in order. Each step is finished on Linux first: its tests, the full test
run, the Linux package and its self-test. Windows follows once every step works on Linux (part 2).

The browser version that was tried out earlier (branch `web`) is a guide here, not a rule: what it learned is
brought into the app where it helps the people who use the app.

## Part 1: Linux

### 1. Saving to your own Backblaze B2 account

The code is in the app already, from the browser version, but the window hides it. To do:

- Show the B2 entry and its setup in the window, on a page of its own: create the account, create a private
  bucket, create a key limited to that bucket, paste the key. Each step gives the page to open first, then what
  to do there. Links open in the person's own browser. The bucket name is suggested and can be changed; the key
  must be for that bucket, and connecting saves the name.
- Before the first step, what Backblaze costs, by the day as its pages show it and by the month: free up to 10 GB,
  the price past that, the card and the daily spending limit, with the person's own library priced as the heading
  of those details. An upload Backblaze stops at a limit says which of its pages lifts it. After the steps, links
  to Backblaze's own pages (files, reports, billing, limits, keys), each saying what it is for; Backblaze offers
  no way for the app to do these itself.
- Bring in the later fix from the browser version: the way to Application Keys in Backblaze's menu. The key step
  links straight to Application Keys, since the person is signed in from creating the bucket.
- The trial before anything is uploaded for real: two saved episodes are uploaded, read back and compared, and
  the test copies deleted. Uploads start only after a passed trial and the person's own choice.
- Each new CBZ is uploaded as it is made; the copy on this computer is removed only once the upload is checked.
  Unfinished uploads can be retried and leftovers of interrupted ones cleaned up, with buttons.
- The library page shows episodes kept only in the cloud, with its filters.
- The key stays on this computer, readable only by its owner.
- Tests: the page in the window, the app's routes, and uploading against a stand-in for B2. No real account is
  used in automated tests; a real account is tried by hand, with its owner.

### 2. Mobile data

Saving waits for an unmetered connection unless the person allows mobile data. Show that choice in the window.

### 3. Choosing the site

- The site list becomes a real choice, kept for the next start. Each site has its own folder inside the library
  folder, with its own sign-in and downloader; Tapas keeps the library folder itself, so nothing already saved
  moves. Switching is refused while saving or signing in, and happens behind the open page, which is not
  reloaded. The library page shows the open site's files.
- A site can be chosen once it works (steps 4 and 5).
- Tapas keeps working exactly as now (its stress run and engine check stay required).

Everything a site decides outside its own reader (the address of its pages, whose cookies a sign-in keeps
and where the downloader sends them, the access marks of its episodes, the account lists Go reads) is in its
profile in `comic_archive/sites.py`. A saved sign-in records its site, and the downloader reads with that
site's reader.

### 4. Webtoon

- Learn how its pages are made: public pages first, then a signed-in library (subscriptions, unlocked
  episodes) with the account's owner at the window.
- Sign-in in the window; list the library; list a series' episodes; save each episode's images as a CBZ with
  its details.
- Tests from recorded public pages. Nothing personal goes into the recordings.
- Found on public pages: episode pages are plain image files. The website shows only part of many series
  ("Read more episodes on the app!"), and paid episodes appear to be readable only in Webtoon's phone app. A
  signed-in look shows what the website gives an account that bought episodes.
- Done: the reader lists a series' episodes from Webtoon's episode list and saves each episode's original
  pictures (not the reduced copies the page shows) with the series' details, for Originals and Canvas series.
  Still to come: sign-in and the subscription list, with the account's owner at the window.

### 5. Tappytoon

- The same steps as Webtoon.
- If Tappytoon protects its images (scrambled or encrypted pages), work stops there until the owner decides:
  undoing such protection is a legal question, not a technical one.
- Found on a free chapter: pages are plain JPEG files, neither scrambled nor encrypted, given only to the
  browser that opened the chapter. The page lays an invisible blank picture over each one, so that "save
  image" in a browser saves the blank; the app reads the real pages the browser loads and undoes nothing.
- Done: the reader lists a series' chapters and saves the free and unlocked ones, reading Tappytoon's API with
  the key its own pages carry, and each chapter's pictures with the cookies its page list hands out, as a
  browser does. Locked chapters are not read.
- Done, from a signed-in look with the account's owner: sign-in in the window (the app recognises the account
  from the number Tappytoon's own pages carry), and Go reads the account's bought, subscribed and recently read
  lists, in that order. Bought and rented chapters are saved; a rented one is noted with the end of its rental.
  Tappytoon can be chosen in the app. Checked with a real account: its bought comics listed and a bought chapter
  saved. The site sometimes asks for a captcha at sign-in; the person answers it in the window.

## Part 2: Windows

### 6. Everything on Windows

- Cloud saving on Windows: its upload helper for Windows, pinned like the Linux one. Done: the Windows app
  fetches and checks the pinned Windows helper, runs it without a console window, and its cloud tests pass in
  the Windows test machine.
- All tests in the Windows test machine, the Windows package and its installer. Done: every test passes there
  (those that need Linux's permission bits or signals are skipped), and the installer installs, starts and
  removes the app for an ordinary account.
- By hand in a Windows machine with internet: B2 setup and upload, and each site's sign-in and saving. Done
  without an account: the helper is fetched and checked, and a public Webtoon episode and a free Tappytoon
  chapter are saved. Still to come, with the account's owner: a real B2 upload and each site's sign-in.

## Not planned for now

These were tried in the browser version and hidden there too. Say if one should come:

- Google Drive, Dropbox and Proton Drive as cloud storage.
- Choosing single series, image quality, a daily schedule and the library report on the main screen.
