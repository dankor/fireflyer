"""Groups — the names, the `group:dataset` references and the URLs.

Every dataset and dashboard lives in a group, and a name is unique only inside
its group. A chart reads a dataset by **reference**: a bare `orders` means the
dashboard's own group, `sales:orders` names another. A reference is kept in
its **normal form** wherever two are compared — bare inside its own group,
qualified otherwise — so `orders` and `sales:orders` are the same dataset to
a dashboard stored in `sales`.

Pure functions, no store: the portal (`web/`) and the dashboard parser use
them alike.
"""

from __future__ import annotations

import re
from urllib.parse import quote

# A group's and a dataset's name: plain ASCII, so a reference never needs
# quoting and `:` is free to join the two. A leading `_` is a personal group's.
_NAME = re.compile(r"[A-Za-z0-9_-]+")
# A user's name: lowercase latin letters, digits after the first — the
# personal group `_<name>` is then a valid group name too.
_USER = re.compile(r"[a-z][a-z0-9]*")
PERSONAL = "_"
SEPARATOR = ":"
# The admin login's group, whatever FIREFLYER_USER says: where everything from
# before groups went.
ADMIN_GROUP = "_admin"
# The one group of the plain single-dashboard editor (no portal, no paths).
LOCAL_GROUP = "local"


class GroupError(ValueError):
    """A name that breaks the rules."""


def check_name(name: str, kind: str = "group") -> str:
    """`name` trimmed, if it's a valid group or dataset name."""
    name = name.strip()
    if not name:
        raise GroupError(f"a {kind} needs a name")
    if not _NAME.fullmatch(name):
        raise GroupError(f"a {kind} name can hold only letters, digits, _ and - ({name!r})")
    return name


def check_group_name(name: str) -> str:
    """A shared group's name: valid, and not a personal group's."""
    name = check_name(name)
    if name.startswith(PERSONAL):
        raise GroupError("a name starting with _ is a personal group's")
    return name


def check_user_name(name: str) -> str:
    name = name.strip()
    if not _USER.fullmatch(name):
        raise GroupError(
            f"a user name is lowercase latin letters, then letters or digits ({name!r})"
        )
    return name


def personal(user: str) -> str:
    """A user's own group."""
    return PERSONAL + user


def is_personal(group: str) -> bool:
    return group.startswith(PERSONAL)


# --- references -------------------------------------------------------------------


def split(ref: str, home: str) -> tuple[str, str]:
    """`(group, dataset)` a reference names, read from a dashboard in `home`."""
    group, sep, name = ref.partition(SEPARATOR)
    return (group, name) if sep else (home, ref)


def join(group: str, name: str, home: str) -> str:
    """The normal form of `group`'s dataset `name`, written in `home`."""
    return name if group == home else f"{group}{SEPARATOR}{name}"


def normal(ref: str, home: str) -> str:
    return join(*split(ref, home), home)


def is_qualified(ref: str) -> bool:
    return SEPARATOR in ref


# --- URLs -------------------------------------------------------------------------
# A dashboard's name is free text and sits in its URL, `/d/<group>/<name>`.
# Percent-encoding isn't enough: routing decodes `%2F` back into `/` before it
# matches, so those characters are escaped first, `~` and two hex digits, as
# `~` itself is. A leading dot too, so `.` and `..` never read as a path step.

_ESCAPED = {"~": "~7E", "/": "~2F", "\\": "~5C"}


def url_name(name: str) -> str:
    """`name` as one URL path segment."""
    escaped = "".join(_ESCAPED.get(c, c) for c in name)
    if escaped.startswith("."):
        escaped = "~2E" + escaped[1:]
    return quote(escaped, safe="")


def from_url(segment: str) -> str:
    """The name a `url_name` segment stands for — the segment as routing hands
    it over, percent-decoded already."""
    return re.sub(r"~([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), segment)


def dashboard_url(group: str, name: str) -> str:
    return f"/d/{url_name(group)}/{url_name(name)}"


def dataset_url(group: str, name: str) -> str:
    return f"/datasets/{url_name(group)}/{url_name(name)}"
