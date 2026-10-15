"""Stream a multipart/form-data body into an `UploadSession` within the upload limits."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, BinaryIO

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from awa.data import TooLargeError, TooManyFilesError, UnsupportedTypeError, safe_name
from awa.server._web import Request
from awa.server.errors import InvalidRequestError, UploadTimeoutError
from awa.server.ports import UploadSession

if TYPE_CHECKING:
    from python_multipart.multipart import MultipartCallbacks

FILES_FIELD = b"files"
"""The only form field the files of an upload may be sent in."""

_MAX_FIELD_BYTES = 64 * 1024
_MAX_PART_HEADER_BYTES = 8 * 1024
"""The headers of one part (`Content-Disposition`, `Content-Type`) are short; more is refused."""
_FRAMING_SLACK = 1024 * 1024


@dataclass(frozen=True)
class ReceiveLimits:
    """What one upload request may be: sizes in bytes, counts, and seconds."""

    max_file_bytes: int
    max_request_bytes: int
    max_files: int
    idle_timeout: float
    upload_timeout: float

    @property
    def body_limit(self) -> int:
        """The largest request body worth reading: the files plus multipart framing."""

        return self.max_request_bytes + _FRAMING_SLACK


@dataclass
class _Receiver:
    """The callbacks of the multipart parser: stream every file part to the session."""

    session: UploadSession
    limits: ReceiveLimits
    names: set[str] = field(default_factory=set)
    files: int = 0
    parts: int = 0
    total: int = 0
    other: int = 0
    ended: bool = False
    handle: BinaryIO | None = None
    size: int = 0
    name: str = ""
    _headers: dict[bytes, bytes] = field(default_factory=dict)
    _field: bytes = b""
    _value: bytes = b""
    _header_bytes: int = 0

    def callbacks(self) -> MultipartCallbacks:
        return {
            "on_part_begin": self.part_begin,
            "on_header_field": self.header_field,
            "on_header_value": self.header_value,
            "on_header_end": self.header_end,
            "on_headers_finished": self.headers_finished,
            "on_part_data": self.data,
            "on_part_end": self.close,
            "on_end": self.end,
        }

    def part_begin(self) -> None:
        self.parts += 1
        if self.parts > 2 * self.limits.max_files + 20:  # a body of nothing but empty parts
            raise TooManyFilesError(
                "The upload has far more parts than files; send each file as one part, "
                f"at most {self.limits.max_files} files"
            )
        self._headers = {}
        self._header_bytes = 0

    def _count_header(self, amount: int) -> None:
        self._header_bytes += amount
        if self._header_bytes > _MAX_PART_HEADER_BYTES:
            raise InvalidRequestError(
                f"The headers of a part of the upload are longer than {_MAX_PART_HEADER_BYTES} "
                "bytes; send each file as a plain part with a short file name"
            )

    def header_field(self, data: bytes, start: int, end: int) -> None:
        self._count_header(end - start)
        self._field += data[start:end]

    def header_value(self, data: bytes, start: int, end: int) -> None:
        self._count_header(end - start)
        self._value += data[start:end]

    def header_end(self) -> None:
        self._headers[self._field.lower()] = self._value
        self._field = self._value = b""

    def headers_finished(self) -> None:
        _, options = parse_options_header(self._headers.get(b"content-disposition", b""))
        raw = options.get(b"filename")
        if not raw:  # a form field, or an empty file input: nothing to keep
            self.handle = None
            return
        field = options.get(b"name", b"")
        if field != FILES_FIELD:
            shown = field.decode("utf-8", errors="replace")
            raise InvalidRequestError(
                f"Send every file in the form field named `files`, not {shown!r}: other field "
                "names are not accepted"
            )
        if self.files >= self.limits.max_files:
            raise TooManyFilesError(
                f"More than {self.limits.max_files} files in one upload; split them over "
                "several uploads"
            )
        self.files += 1
        self.size = 0
        self.name = self._unique(safe_name(raw.decode("utf-8", errors="replace")))
        self.handle = self.session.open(self.name)

    def _unique(self, name: str) -> str:
        stem, dot, extension = name.rpartition(".")
        if not dot:
            stem, extension = name, ""
        tail = f".{extension}" if extension else ""
        candidate, number = name, 1
        while candidate.lower() in self.names:
            number += 1
            base = stem
            # Shorten the stem until the numbered name is still a safe one (at most 100 chars).
            while (candidate := f"{base}-{number}{tail}") != safe_name(candidate) and base:
                base = base[:-1]
        self.names.add(candidate.lower())
        return candidate

    def data(self, data: bytes, start: int, end: int) -> None:
        amount = end - start
        if self.handle is None:
            self.other += amount
            if self.other > _MAX_FIELD_BYTES:
                raise InvalidRequestError(
                    "A form field of the upload is too large; send only the files"
                )
            return
        self.size += amount
        if self.size > self.limits.max_file_bytes:
            raise TooLargeError(
                f"{self.name} is larger than {self.limits.max_file_bytes} bytes, the limit "
                "for one file; split it into smaller files"
            )
        self.total += amount
        if self.total > self.limits.max_request_bytes:
            raise TooLargeError(_too_large(self.limits))
        self.session.count(amount)
        self.handle.write(data[start:end])

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def end(self) -> None:
        self.ended = True


def _too_large(limits: ReceiveLimits) -> str:
    return (
        f"The upload is larger than {limits.max_request_bytes} bytes; send the files in "
        "several uploads"
    )


def _boundary(request: Request) -> bytes:
    kind, options = parse_options_header(request.headers.get("content-type", ""))
    boundary = options.get(b"boundary", b"")
    if kind != b"multipart/form-data" or not boundary:
        raise UnsupportedTypeError(
            "Send the files as multipart/form-data (one part per file); other content types "
            "are not accepted"
        )
    return boundary


async def receive_files(request: Request, session: UploadSession, limits: ReceiveLimits) -> int:
    """Stream the files of `request` into `session`; return how many were received.

    Sizes, counts, quota and timeouts are enforced while streaming; client file names are used
    only through `awa.data.safe_name`.

    Raises:
        UnsupportedTypeError: The request is not multipart/form-data.
        TooLargeError: A file or the request is larger than allowed.
        TooManyFilesError: More files than allowed.
        QuotaExceededError: The session's quota is used up.
        UploadTimeoutError: The client was idle or too slow.
        InvalidRequestError: Malformed, incomplete or empty body.
    """

    boundary = _boundary(request)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limits.body_limit:
        raise TooLargeError(_too_large(limits))
    receiver = _Receiver(session, limits)
    try:
        parser = MultipartParser(boundary, receiver.callbacks())
        await _feed(request, parser, limits)
    except MultipartParseError as error:
        raise InvalidRequestError("The upload is not well-formed multipart/form-data") from error
    finally:
        receiver.close()
    if not receiver.ended:
        raise InvalidRequestError("The upload is incomplete: the body ended early")
    if receiver.files == 0:
        raise InvalidRequestError("The upload holds no file")
    return receiver.files


async def _feed(request: Request, parser: MultipartParser, limits: ReceiveLimits) -> None:
    stream = request.stream()
    try:
        async with asyncio.timeout(limits.upload_timeout):
            while True:
                try:
                    async with asyncio.timeout(limits.idle_timeout):
                        chunk = await anext(stream)
                except StopAsyncIteration:
                    break
                if chunk:
                    parser.write(chunk)
    except TimeoutError as error:
        raise UploadTimeoutError(
            "The upload stalled or took too long; send it again over a faster connection"
        ) from error
    finally:
        with contextlib.suppress(Exception):
            await stream.aclose()
