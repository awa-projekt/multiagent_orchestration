"""Errors of `awa.data`: bad arguments and refused files, each with a stable code."""

from __future__ import annotations

from typing import ClassVar

from awa.errors import AwaError


class DataError(AwaError, ValueError):
    """Base class of every `awa.data` error (bad arguments, refused files).

    A bad file never raises in `profile()`: it gets `status: "error"` and a flag. `code` is the code
    the HTTP API reports.
    """

    code: ClassVar[str] = "awa.data"


class UnsupportedTypeError(DataError):
    """The file kind is not one `awa.data` reads (extension and content are both unknown)."""

    code = "awa.data.unsupported_type"


class TypeMismatchError(DataError):
    """The content does not agree with the file name (for example a `.pdf` that is not a PDF)."""

    code = "awa.data.type_mismatch"


class TooLargeError(DataError):
    """A file is larger than `Limits.max_file_bytes` or the files together exceed the total."""

    code = "awa.data.too_large"

    @classmethod
    def file(cls, name: str, limit: int) -> TooLargeError:
        """One file above `limit` bytes; the message names the way out."""

        return cls(
            f"{name}: more than {limit} bytes; split the file or raise Limits(max_file_bytes)"
        )

    @classmethod
    def together(cls, limit: int) -> TooLargeError:
        """All files above `limit` bytes together; the message names the way out."""

        return cls(
            f"the files exceed {limit} bytes together; split the data or raise Limits(max_total_bytes)"
        )


class TooManyFilesError(DataError):
    """More files than `Limits.max_files`."""

    code = "awa.data.too_many_files"

    @classmethod
    def over(cls, limit: int, count: int | None = None) -> TooManyFilesError:
        """More than `limit` files (`count` when known); the message names the way out."""

        found = (
            f"{count} files; at most {limit} are allowed" if count else f"more than {limit} files"
        )
        return cls(f"{found}; use fewer files or raise Limits(max_files)")


class UnsafeArchiveError(DataError):
    """A zip archive that is unsafe to unpack (path escape, link, too many entries, bomb)."""

    code = "awa.data.zip_unsafe"


class PathNotAllowedError(DataError):
    """A path outside the allowed `root`, a missing path, or a symbolic link."""

    code = "awa.data.path_not_allowed"


class RowLimitError(DataError):
    """The table has more rows than the caller is willing to read (raised while iterating)."""

    code = "awa.data.row_limit"
