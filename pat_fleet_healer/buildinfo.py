"""Build identity, STAMPED BY build.py — never hand-edited.

Why this exists: __version__ is hand-maintained, so two different artifacts can both
claim the same number. That is not hypothetical — on 2026-09-22 dist/ shipped a v529
artifact while the source said 531, because the rebuilt dist was never committed. Every
node would have reported sw=531 and been wrong. A node must be able to say WHICH build
it runs, not just which version number someone typed.

build.py overwrites this file in the staging copy at build time; the values below are
the "unstamped source tree" fallback, which is itself useful information.
"""
COMMIT = "unstamped"      # git short SHA the artifact was built from
BUILT_AT = "unstamped"    # UTC ISO-8601 build timestamp
DIRTY = None              # True if the worktree had uncommitted changes at build time


def describe(version):
    """Compact identity for logs and the heartbeat: 531+g22f7b41 (or 531+gabc1234-dirty)."""
    if COMMIT == "unstamped":
        return "%s+unstamped" % version
    return "%s+g%s%s" % (version, COMMIT, "-dirty" if DIRTY else "")
