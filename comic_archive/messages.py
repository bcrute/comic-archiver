"""Every phrase the apps show a person, by code: the desktop window and the phone say the same words.

The words are in messages.json, beside this file. The phone's screens read that same file, so a phrase is
written once. A phrase may name values in braces, as in 'Sign in to {site}'.
"""
import json
import pkgutil

TEXT = json.loads(pkgutil.get_data('comic_archive', 'messages.json').decode('utf-8'))


def say(code, **values):
    """The phrase for a code, with its named values filled in."""
    words = TEXT[code]
    for name, value in values.items():
        words = words.replace('{' + name + '}', str(value))
    return words
