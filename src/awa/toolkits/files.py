"""File tools confined to one directory: list, read, write, append, move and delete.

Paths resolve inside `root` through a `Sandbox` (no absolute paths, `..`, devices, hidden
entries or links out of the root); writes are atomic, size-capped and wait for approval by
default.
"""

from __future__ import annotations

import os
import tempfile
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path, PurePath

from pydantic import Field

from awa._internal.sandbox import Sandbox
from awa._internal.upstream.tools import BaseTool, ToolException
from awa.toolkits._common import (
    DEFAULT_MAX_OUTPUT_CHARS,
    ToolArgs,
    ToolSpecs,
    positive,
    selected,
    toolkit_tools,
)
from awa.tools.result_cache import DATA_ROOT_METADATA_KEY

FILE_TOOLS = ("list_files", "read_file", "write_file", "append_file", "move_file", "delete_file")
"""Names of the tools `file_toolkit` builds, in order."""

READ_TOOLS = ("list_files", "read_file")

_BINARY_PROBE = 8_192


class ListFilesArgs(ToolArgs):
    path: str = Field(default=".", description="Directory to list, relative to the root.")
    pattern: str = Field(default="*", description="Glob pattern on file names, e.g. '*.csv'.")
    recursive: bool = Field(default=False, description="Include subdirectories.")
    max_entries: int = Field(default=200, ge=1, le=5_000, description="Most entries returned.")


class ReadFileArgs(ToolArgs):
    path: str = Field(description="File to read, relative to the root.")
    start_line: int = Field(default=1, ge=1, description="First line to return (1-based).")
    max_lines: int | None = Field(default=None, ge=1, description="Most lines to return.")


class WriteFileArgs(ToolArgs):
    path: str = Field(description="File to write, relative to the root.")
    content: str = Field(description="The complete new text of the file.")
    overwrite: bool = Field(default=False, description="Replace the file if it exists.")


class AppendFileArgs(ToolArgs):
    path: str = Field(description="File to append to (created if missing), relative to the root.")
    content: str = Field(description="Text to add at the end of the file.")


class MoveFileArgs(ToolArgs):
    source: str = Field(description="File or directory to move, relative to the root.")
    destination: str = Field(description="New path, relative to the root.")
    overwrite: bool = Field(default=False, description="Replace an existing destination file.")


class DeleteFileArgs(ToolArgs):
    path: str = Field(description="File to delete, relative to the root. Directories are refused.")


@dataclass(frozen=True)
class _FileTools:
    sandbox: Sandbox
    max_read_bytes: int
    max_write_bytes: int
    encoding: str

    def list_files(self, args: ListFilesArgs) -> str:
        base = self.sandbox.resolve(args.path, must_exist=True)
        if not base.is_dir():
            raise ToolException(f"{args.path!r} is not a directory")
        lines: list[str] = []
        truncated = False
        for entry in self._walk(base, args.recursive):
            line = self._line(entry, args.pattern)
            if line is None:
                continue
            if len(lines) >= args.max_entries:  # one more entry exists: the list is cut
                truncated = True
                break
            lines.append(line)
        if not lines:
            return f"No entries in {self.sandbox.relative(base)!r} match {args.pattern!r}."
        if truncated:
            lines.append(f"… (stopped after {args.max_entries} entries)")
        return "\n".join(lines)

    def _line(self, entry: Path, pattern: str) -> str | None:
        """The listing line of `entry`, or `None` when it does not match `pattern`."""

        if entry.is_dir():
            if pattern in ("*", "") or fnmatch(entry.name, pattern):
                return f"{self.sandbox.relative(entry)}/"
            return None
        if not fnmatch(entry.name, pattern or "*"):
            return None
        stat = entry.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, UTC).strftime("%Y-%m-%d %H:%M")
        return f"{self.sandbox.relative(entry)}  {stat.st_size} bytes  {modified}"

    def _walk(self, directory: Path, recursive: bool) -> Iterator[Path]:
        """Entries breadth-first, lazily, so a listing stops at its entry limit."""

        pending = deque([directory])
        while pending:
            current = pending.popleft()
            try:
                children = sorted(current.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                continue
            for child in children:
                resolved = child.resolve(strict=False)
                if not self.sandbox.contains(resolved) or self.sandbox.hidden(child):
                    continue  # a link leading out of the root, or a hidden entry
                yield child
                is_link = resolved != child.absolute() or child.is_symlink()
                if recursive and child.is_dir() and not is_link:
                    pending.append(child)

    def read_file(self, args: ReadFileArgs) -> str:
        path = self.sandbox.resolve(args.path, must_exist=True)
        if not path.is_file():
            raise ToolException(f"{args.path!r} is not a file")
        with path.open("rb") as handle:
            data = handle.read(self.max_read_bytes + 1)
        if b"\x00" in data[:_BINARY_PROBE]:
            raise ToolException(f"{args.path!r} is a binary file, not text")
        truncated = len(data) > self.max_read_bytes
        text = data[: self.max_read_bytes].decode(self.encoding, errors="replace")
        text = text.replace("\r\n", "\n")
        if args.start_line > 1 or args.max_lines is not None:
            lines = text.splitlines(keepends=True)
            end = None if args.max_lines is None else args.start_line - 1 + args.max_lines
            text = "".join(lines[args.start_line - 1 : end])
        if truncated:
            text += f"\n… [file larger than {self.max_read_bytes} bytes; the rest was not read]"
        return text

    def _checked_content(self, content: str) -> bytes:
        data = content.encode(self.encoding)
        if len(data) > self.max_write_bytes:
            raise ToolException(
                f"content is {len(data)} bytes, more than the limit of {self.max_write_bytes}"
            )
        return data

    def _writable(self, path_text: str) -> Path:
        path = self.sandbox.resolve(path_text)
        if path == self.sandbox.root or path.is_dir():
            raise ToolException(f"{path_text!r} is a directory")
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if not self.sandbox.contains(parent.resolve(strict=True)):
            raise ToolException(f"path {path_text!r} is outside the allowed directory")
        return path

    def write_file(self, args: WriteFileArgs) -> str:
        data = self._checked_content(args.content)
        path = self._writable(args.path)
        if path.exists() and not args.overwrite:
            raise ToolException(f"{args.path!r} exists; set overwrite to replace it")
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".awa-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return f"Wrote {len(data)} bytes to {self.sandbox.relative(path)}."

    def append_file(self, args: AppendFileArgs) -> str:
        data = self._checked_content(args.content)
        path = self._writable(args.path)
        size = path.stat().st_size if path.exists() else 0
        if size + len(data) > self.max_write_bytes:
            raise ToolException(
                f"the file would grow to {size + len(data)} bytes, more than the limit of "
                f"{self.max_write_bytes}"
            )
        with path.open("ab") as handle:
            handle.write(data)
        return f"Appended {len(data)} bytes to {self.sandbox.relative(path)}."

    def _entry(self, path_text: str) -> Path:
        """The existing entry `path_text` names; a link itself, not the file it points to."""

        resolved = self.sandbox.resolve(path_text, must_exist=True)
        name = PurePath(path_text).name
        if name in ("", ".", ".."):
            return resolved
        return self.sandbox.resolve(str(PurePath(path_text).parent)) / name

    def move_file(self, args: MoveFileArgs) -> str:
        source = self._entry(args.source)
        if source == self.sandbox.root:
            raise ToolException("the root directory cannot be moved")
        destination = self._writable(args.destination)
        if destination.exists() and (not args.overwrite or destination.is_dir() or source.is_dir()):
            raise ToolException(f"{args.destination!r} exists; set overwrite to replace a file")
        os.replace(source, destination)
        return f"Moved {self.sandbox.relative(source)} to {self.sandbox.relative(destination)}."

    def delete_file(self, args: DeleteFileArgs) -> str:
        path = self._entry(args.path)
        if not path.is_file():
            raise ToolException(f"{args.path!r} is not a file; only files can be deleted")
        path.unlink()
        return f"Deleted {self.sandbox.relative(path)}."


def file_toolkit(
    root: str | os.PathLike[str],
    *,
    read_only: bool = False,
    include: Sequence[str] | None = None,
    requires_approval: bool = True,
    allow_hidden: bool = False,
    max_read_bytes: int = 1_000_000,
    max_write_bytes: int = 1_000_000,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    encoding: str = "utf-8",
    frame_output: bool = True,
) -> list[BaseTool]:
    """Build the file tools, confined to `root`.

    | Tool | Does | Approval |
    | --- | --- | --- |
    | `list_files` | lists a directory (optionally recursive, glob filter) | no |
    | `read_file` | reads a text file (line window, size cap) | no |
    | `write_file` | writes a file atomically (refuses to overwrite unless asked) | yes |
    | `append_file` | appends text, creating the file if missing | yes |
    | `move_file` | moves or renames a file or directory | yes |
    | `delete_file` | deletes one file (never a directory) | yes |

    Args:
        root: The directory the tools work in; it must exist.
        read_only: Build only `list_files` and `read_file`.
        include: Build only these tools (names from `FILE_TOOLS`).
        requires_approval: Make the writing tools wait for a human decision (the run needs a
            checkpointer). Set `False` only where every write is acceptable without review.
        allow_hidden: Allow names starting with `.` (`.env`, `.git`).
        frame_output: Label file contents as external data, since documents can contain
            injected instructions.

    Raises:
        ConfigurationError: If `root` is not a directory, a limit is not positive, or `include`
            names an unknown tool.

    Example:
        ```python
        tools = file_toolkit("./inbox", include=["list_files", "read_file", "move_file"])
        ```
    """

    positive(max_read_bytes, "max_read_bytes")
    positive(max_write_bytes, "max_write_bytes")
    names = selected(READ_TOOLS if read_only else FILE_TOOLS, include, "files")
    impl = _FileTools(
        Sandbox(root, allow_hidden=allow_hidden),
        max_read_bytes=max_read_bytes,
        max_write_bytes=max_write_bytes,
        encoding=encoding,
    )
    specs: ToolSpecs = {
        "list_files": (
            impl.list_files,
            ListFilesArgs,
            "List the files and folders in a directory of the shared file area (paths "
            "relative to its root), with sizes and modification times. Use it to discover "
            "which files exist before reading them.",
        ),
        "read_file": (
            impl.read_file,
            ReadFileArgs,
            "Read a text file from the shared file area (path relative to its root). Use "
            "start_line/max_lines for a window of a long file. Returns the file's text; "
            "binary files are refused.",
        ),
        "write_file": (
            impl.write_file,
            WriteFileArgs,
            "Create a text file in the shared file area with the given content. Refuses "
            "to replace an existing file unless overwrite is true. Needs human approval.",
        ),
        "append_file": (
            impl.append_file,
            AppendFileArgs,
            "Append text to the end of a file in the shared file area, creating it if it "
            "does not exist. Use it for logs and running notes. Needs human approval.",
        ),
        "move_file": (
            impl.move_file,
            MoveFileArgs,
            "Move or rename a file or folder inside the shared file area. Refuses to "
            "replace an existing file unless overwrite is true. Needs human approval.",
        ),
        "delete_file": (
            impl.delete_file,
            DeleteFileArgs,
            "Delete one file from the shared file area (folders are never deleted). "
            "Needs human approval.",
        ),
    }
    data_root = {DATA_ROOT_METADATA_KEY: str(Path(root).expanduser().resolve())} if root else None
    return toolkit_tools(
        "files",
        names,
        specs,
        requires_approval=requires_approval,
        framed=("read_file",) if frame_output else (),
        metadata=data_root,
        max_output_chars=max_output_chars,
    )


__all__ = ["FILE_TOOLS", "file_toolkit"]
