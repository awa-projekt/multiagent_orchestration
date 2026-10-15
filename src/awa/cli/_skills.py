"""`awa skills list|show`: the builtin skills and those of extra directories."""

from __future__ import annotations

import argparse

from awa.cli._common import EXIT_OK, Commands, add_command, preview, print_table


def add_skills(commands: Commands) -> None:
    skills = add_command(commands, "skills")
    actions = skills.add_subparsers(dest="skills_command", required=True, metavar="action")
    listing = actions.add_parser("list", help="list the available skills")
    listing.add_argument("--skills", action="append", default=[], help="extra skill directory")
    listing.set_defaults(handler=_skills_list)
    detail = actions.add_parser("show", help="print a skill")
    detail.add_argument("name", help="skill name")
    detail.add_argument("--skills", action="append", default=[], help="extra skill directory")
    detail.set_defaults(handler=_skills_show)


def _skills_list(args: argparse.Namespace) -> int:
    from awa.blueprints._resolve import skill_registry

    registry = skill_registry(args.skills)
    print_table(
        [("skill", "description")]
        + [(name, preview(description, 90)) for name, description in registry.index()]
    )
    return EXIT_OK


def _skills_show(args: argparse.Namespace) -> int:
    from awa.blueprints._resolve import skill_registry

    skill = skill_registry(args.skills).get(args.name)
    print(f"{skill.name} {skill.version} (risk: {skill.risk})")
    print(skill.description)
    if skill.tags:
        print(f"tags: {', '.join(skill.tags)}")
    if skill.resources:
        print(f"resources: {', '.join(skill.resources)}")
    print()
    print(skill.instructions.rstrip())
    return EXIT_OK
