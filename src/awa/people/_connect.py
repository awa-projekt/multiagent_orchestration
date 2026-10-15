"""Where the Microsoft connectors get their `role:` recipients from (the wiring, no logic)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from awa.errors import ConfigurationError
from awa.people._entra import EntraPeople
from awa.people._file import PEOPLE_FILE, PeopleFile
from awa.people._recipients import RoleRecipients, allowed_roles
from awa.people._ref import PeopleError

if TYPE_CHECKING:
    from awa.connectors._engine import ConnectorContext

PEOPLE_FILE_OPTION = "people_file"
"""The connector option naming the people file (default `config/people.yaml`)."""
RECIPIENTS_OPTION = "recipients"
"""The connector option holding the roles that are the exact allow-list of recipients."""


def connector_recipients(ctx: ConnectorContext) -> RoleRecipients:
    """The role resolver of a connector instance: its people file (option `people_file`, else
    `config/people.yaml` of the working directory) and its Entra tenant, reached with the
    instance's own sign-in, variables and options."""

    file = PeopleFile(str(ctx.option(PEOPLE_FILE_OPTION) or PEOPLE_FILE))
    return RoleRecipients(file, {"entra": EntraPeople.from_context(ctx)})


def check_people_options(instance: str, options: Mapping[str, Any]) -> None:
    """Refuse a `recipients` option that is not roles and a `people_file` that is not a path.

    Raises:
        ConfigurationError: Naming the instance and the fix.
    """

    try:
        allowed_roles(options.get(RECIPIENTS_OPTION))
    except PeopleError as exc:
        raise ConfigurationError(f"connector {instance!r}: {exc}") from None
    people_file = options.get(PEOPLE_FILE_OPTION)
    if people_file is not None and not (isinstance(people_file, str) and people_file.strip()):
        raise ConfigurationError(
            f"connector {instance!r}: people_file must be the path of config/people.yaml"
        )
