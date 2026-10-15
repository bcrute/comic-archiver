@echo off
rem Starts Comic Archiver from this folder when it was unpacked from the zip instead of installed.
start "" "%~dp0python\pythonw.exe" -I -X utf8 -m comic_archive.desktop_app %*
