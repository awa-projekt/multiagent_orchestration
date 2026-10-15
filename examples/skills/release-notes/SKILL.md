---
name: release-notes
description: >-
  Turns a list of merged changes into user-facing release notes grouped by impact, with
  breaking changes and required actions first. Use when asked to write release notes, a
  changelog entry, or an announcement of a new version.
metadata:
  version: "1.0.0"
  category: writing
  tags: release-notes changelog versioning announcement
  risk: low
---
# Release Notes

1. Read `references/style-guide.md` before writing; it defines the sections and tone.
2. Sort every change into exactly one section. Drop internal-only changes (refactors,
   CI, test-only changes) unless they change behavior users can observe.
3. Write each entry from the user's point of view: what they can do now, or what they
   must do. Name the affected feature, not the code.
4. Put breaking changes first and give each one a concrete migration step.
5. End with the version number and release date in the format the style guide gives.
