Comic Archiver for macOS

Copy Comic Archiver.app to Applications and open it. Python and Qt are included.
Your saved comics and novels remain in the library folder you choose on the dashboard.
Install a CBZ/EPUB reader and associate those file types with it to use Open.

For an update, close Comic Archiver and replace the app. Keep your library and
settings; there is no need to uninstall or sign in again.

This build is ad-hoc signed and is not notarized by Apple. Native
testing covers Intel macOS 15; Apple Silicon execution remains unqualified.
If macOS blocks it as an unidentified developer, review the download and follow
Apple's guidance: https://support.apple.com/en-us/102445

To remove the app's sign-ins, cloud keys and settings, close it and run this
command in Terminal before deleting the app:

"/Applications/Comic Archiver.app/Contents/MacOS/ComicArchiver" --uninstall

It asks before removing anything. Connect any library drives it requests.
Your saved episodes stay in place. You may then delete Comic Archiver.app.

The app includes third-party licenses and an exact dependency audit in
Contents/Resources. See THIRD-PARTY-NOTICES.md there for replacing Qt.
