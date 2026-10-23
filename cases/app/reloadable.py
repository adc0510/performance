"""Reloaded by c11_reload; VERSION is bumped on disk between loads."""

VERSION = 1


def version():
    return VERSION


def describe():
    return f"v{version()}"
