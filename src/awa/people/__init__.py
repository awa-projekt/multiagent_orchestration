"""People directory (extension): find colleagues, pin them to a project, use them by role.

Preview: it is tested against a fake Microsoft Graph only, and its API may still change.

`EntraPeople` searches Entra ID, `LocalPeople` a CSV or YAML list; `PeopleFile` keeps the pinned
people in `config/people.yaml` (out of git) and `RoleRecipients` resolves `role:<name>` at send time.
"""

from typing import TYPE_CHECKING, Any

from awa._internal.util import lazy_attribute
from awa.people._book import PeopleBook
from awa.people._file import PEOPLE_FILE, PeopleFile, ensure_gitignored
from awa.people._local import LocalPeople
from awa.people._recipients import ROLE_PREFIX, RoleRecipients, allowed_roles, role_name
from awa.people._ref import MAX_RESULTS, PeopleError, PersonRef, clean_search_text
from awa.people.ports import PeopleDirectory

if TYPE_CHECKING:  # loaded on use: it brings the Microsoft connector
    from awa.people._entra import EntraPeople

__stability__ = "preview"

__all__ = [
    "MAX_RESULTS",
    "PEOPLE_FILE",
    "ROLE_PREFIX",
    "EntraPeople",
    "LocalPeople",
    "PeopleBook",
    "PeopleDirectory",
    "PeopleError",
    "PeopleFile",
    "PersonRef",
    "RoleRecipients",
    "allowed_roles",
    "clean_search_text",
    "ensure_gitignored",
    "role_name",
]


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), {"EntraPeople": "awa.people._entra"}, name)
