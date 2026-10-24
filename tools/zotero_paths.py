# -*- coding: utf-8 -*-
"""Locate the Zotero profile and the retrieval index directory.

The helper works on Windows (default), macOS and Linux, and can be overridden with
environment variables:

    ZOTERO_PROFILE          absolute path to the Zotero profile directory
    RETRIEVAL_INDEX_DIR     absolute path to the index directory (highest priority)

If no profile can be found, ``find_index_dir`` raises a short, actionable error.
"""

import glob
import os
import sys


def find_profile_dir():
    """Return the Zotero profile directory that contains preferences."""
    override = os.environ.get("ZOTERO_PROFILE")
    if override:
        return os.path.abspath(override)

    home = os.path.expanduser("~")
    candidates = []
    if os.name == "nt" or os.environ.get("APPDATA"):
        base = os.path.join(os.environ.get("APPDATA", ""), "Zotero", "Zotero", "Profiles")
        candidates += glob.glob(os.path.join(base, "*"))
    candidates += glob.glob(os.path.join(home, ".zotero", "zotero", "*"))
    candidates += glob.glob(os.path.join(home, "Zotero", "Profiles", "*"))

    profiles = [p for p in candidates if os.path.isfile(os.path.join(p, "prefs.js"))]
    if not profiles:
        print(
            "warning: no Zotero profile found; set ZOTERO_PROFILE or RETRIEVAL_INDEX_DIR",
            file=sys.stderr,
        )
        return os.path.join(home, "Zotero", "Profiles", "default")
    # Prefer the most recently used profile when several exist.
    profiles.sort(key=lambda p: os.path.getmtime(os.path.join(p, "prefs.js")), reverse=True)
    return profiles[0]


def find_index_dir():
    """Return the retrieval index directory, honouring RETRIEVAL_INDEX_DIR."""
    override = os.environ.get("RETRIEVAL_INDEX_DIR")
    if override:
        return os.path.abspath(override)
    return os.path.join(find_profile_dir(), "retrieval-index")


def find_sidecar_dir(name):
    """Return a sidecar directory inside the index (e.g. 'ocr', 'tables')."""
    return os.path.join(find_index_dir(), name)
