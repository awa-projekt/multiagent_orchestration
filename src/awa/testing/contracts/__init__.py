"""Contract suites for the backend protocols of awa: one suite per protocol, run by every backend of it.

Subclass a protocol's suite and say how to open your backend; it is held to the same behavior as
awa's own backends:

```python
class TestMyQueue(EventQueueContract):
    capabilities = EventQueueCapabilities(survives_restart=True)

    def opened(self, place, *, name="q", max_attempts=5, clock=None):
        return my_queue_context(place.path, name, max_attempts)   # an async context manager
```

`place` (a `Place`) holds one test's folder, unique name and DSN. Behavior a backend may lack is
a capability flag (the test is skipped); an undecided difference goes into `known_deviations`
(expected to fail, strictly). The suites need `pytest` and `pytest-asyncio`; `awa.testing`
itself never imports pytest.
"""

from typing import TYPE_CHECKING, Any

from awa._internal.util import homes, lazy_attribute
from awa.testing.contracts._base import AdapterContract, Place
from awa.testing.contracts.artifact_repository import (
    ArtifactRepositoryCapabilities,
    ArtifactRepositoryContract,
)
from awa.testing.contracts.audit_sink import AuditSinkCapabilities, AuditSinkContract
from awa.testing.contracts.cache_store import CacheStoreContract
from awa.testing.contracts.checkpointer import CheckpointerCapabilities, CheckpointerContract
from awa.testing.contracts.erasure import ErasureContract, ErasureStack
from awa.testing.contracts.run_log import RunLogContract
from awa.testing.contracts.thread_index import ThreadIndexCapabilities, ThreadIndexContract

if TYPE_CHECKING:
    from awa.testing.contracts.build_store import BuildStoreContract
    from awa.testing.contracts.event_queue import EventQueueCapabilities, EventQueueContract
    from awa.testing.contracts.file_store import FileStoreContract
    from awa.testing.contracts.notifier import NotifierContract, NotifierUnderTest
    from awa.testing.contracts.run_store import RunStoreCapabilities, RunStoreContract
    from awa.testing.contracts.upload_store import UploadStoreContract

_HOMES = homes(
    {
        "awa.testing.contracts.build_store": "BuildStoreContract",
        "awa.testing.contracts.event_queue": "EventQueueCapabilities EventQueueContract",
        "awa.testing.contracts.file_store": "FileStoreContract",
        "awa.testing.contracts.notifier": "NotifierContract NotifierUnderTest",
        "awa.testing.contracts.run_store": "RunStoreCapabilities RunStoreContract",
        "awa.testing.contracts.upload_store": "UploadStoreContract",
    }
)

__stability__ = "preview"

__all__ = [
    "AdapterContract",
    "ArtifactRepositoryCapabilities",
    "ArtifactRepositoryContract",
    "AuditSinkCapabilities",
    "AuditSinkContract",
    "BuildStoreContract",
    "CacheStoreContract",
    "CheckpointerCapabilities",
    "CheckpointerContract",
    "ErasureContract",
    "ErasureStack",
    "EventQueueCapabilities",
    "EventQueueContract",
    "FileStoreContract",
    "NotifierContract",
    "NotifierUnderTest",
    "Place",
    "RunLogContract",
    "RunStoreCapabilities",
    "RunStoreContract",
    "ThreadIndexCapabilities",
    "ThreadIndexContract",
    "UploadStoreContract",
]


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), _HOMES, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
