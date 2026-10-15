"""Runtime layer: the state of a running run tree, its deadline and its stop button.

`RunContext` / `current_run_context` (shared by a run tree), `cancel` and `RunHandle`,
`case_dir` (the running eval case's folder), `subscribe_globally`, the time and id backends
(`Clock`, `IdSource` and their defaults), the caches (`Cache`, `CacheSettings`,
`CacheStore`) and the engine port (`Engine`, `current_engine`, `use_engine`).
"""

from awa.runtime.ambient import global_subscribers, subscribe_globally, use_global_subscriber
from awa.runtime.cache import Cache, CacheSettings
from awa.runtime.cancellation import RunHandle, RunScope, active_runs, cancel, checkpoint
from awa.runtime.case import active_case_dir, case_dir, use_case_dir
from awa.runtime.clock import Clock, SystemClock, as_clock
from awa.runtime.context import (
    EventEmitter,
    EventSubscriber,
    RunContext,
    current_run_context,
    new_run_id,
    require_run_context,
    use_run_context,
    use_run_id,
)
from awa.runtime.engine import (
    AgentSpec,
    Engine,
    EngineOutcome,
    Pause,
    Resume,
    RunOptions,
    ThreadState,
    current_engine,
    use_engine,
)
from awa.runtime.ids import IdSource, Uuid7Ids, UuidIds
from awa.runtime.ports import CacheKindStats, CacheScope, CacheStore

__all__ = [
    "AgentSpec",
    "Cache",
    "CacheKindStats",
    "CacheScope",
    "CacheSettings",
    "CacheStore",
    "Clock",
    "Engine",
    "EngineOutcome",
    "EventEmitter",
    "EventSubscriber",
    "IdSource",
    "Pause",
    "Resume",
    "RunContext",
    "RunHandle",
    "RunOptions",
    "RunScope",
    "SystemClock",
    "ThreadState",
    "Uuid7Ids",
    "UuidIds",
    "active_case_dir",
    "active_runs",
    "as_clock",
    "cancel",
    "case_dir",
    "checkpoint",
    "current_engine",
    "current_run_context",
    "global_subscribers",
    "new_run_id",
    "require_run_context",
    "subscribe_globally",
    "use_case_dir",
    "use_engine",
    "use_global_subscriber",
    "use_run_context",
    "use_run_id",
]
