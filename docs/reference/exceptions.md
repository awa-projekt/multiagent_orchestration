# Exceptions

Every error awa raises on purpose, its code and whether a retry can help.

```python title="exceptions_example.py"
from awa import Agent, UsageLimitExceededError
from awa.contracts import UsageLimits
from awa.errors import AwaError
from awa.testing import FakeChatModel, tool_call

clerk = Agent("clerk", model=FakeChatModel([tool_call("unknown", {})] * 3, repeat_last=True))
try:
    clerk.run_sync("Loop forever.", usage_limits=UsageLimits(max_requests=2))
except UsageLimitExceededError as error:
    print(isinstance(error, AwaError), error.code, error.retryable)
```

```text title="exceptions_example.out"
True awa.usage_limit_exceeded False
```

Every deliberate error derives from `AwaError`, so one `except AwaError` separates the decisions
of awa (limits, topology, validation) from failures of a provider or a tool. A wrong argument
raises the built-in `ValueError` or `TypeError`. Every error imports from `awa.errors`; the most
used ones also import from `awa`.

```text
AwaError
│ Runs and limits
├── AgentRunError                     a run failed for a reason nothing more specific covers
│   ├── MaxStepsExceededError         more model calls than max_steps
│   ├── OutputRejectedError           an output validator still rejected the answer after output_retries
│   └── StepCheckFailedError          a check of a process step failed
│       └── StepRejectedError         a special case or a person rejected the item (run status rejected)
├── UsageLimitExceededError           the run tree crossed a UsageLimits bound
│   ├── BuildUsageLimitError          a build used up its model call or token limits (data endpoints)
│   └── NoProgressError               the same call or result repeated, or a tool failed again and again
├── RunTimeoutError                   timeout / max_duration exceeded; the whole tree stops (also a TimeoutError)
├── RunCancelledError                 the run was cancelled before it finished
├── StructuredOutputError             the model never returned the declared output_type
├── GuardrailTripwireError            a guardrail blocked input or output (awa.guardrails)
├── MissingPriceError                 no price for a model whose cost is computed
│ Models
├── ModelResolutionError              a model reference cannot be resolved, or real requests are blocked
├── ModelProviderError                a provider request failed and no retry recovered
│   └── ModelRateLimitError           the provider kept answering 429
│ Collaboration
├── TopologyError                     invalid Team or AgentNetwork topology
├── HandoffError                      a handoff was not allowed or exceeded max_handoffs
├── AgentNotFoundError                unknown agent name
├── AgentAlreadyRegisteredError       duplicate name in a directory or on a bus
├── MessagingError
│   ├── RecipientNotFoundError        no mailbox for the recipient
│   ├── CommunicationNotAllowedError  the communication policy forbids the message
│   ├── MailboxFullError              a bounded "reject" mailbox is full
│   ├── MessageRequestTimeoutError    a request received no reply in time
│   └── BlackboardConflictError       a compare-and-set write lost the race
├── TaskError
│   ├── TaskNotFoundError
│   ├── TaskStateError                the task's status does not allow the change
│   │   └── TaskClaimError            owned elsewhere, not ready, or missing capabilities
│   ├── TaskPermissionError           only the owner or creator may change it
│   ├── TaskDependencyError           unknown dependency or a cycle
│   ├── TaskConflictError             expected_version is stale
│   └── TaskTimeoutError              wait_until_done timed out (also a TimeoutError)
├── CacheStoreError                   a cache store could not read or write (awa carries on without the cache)
├── CallCycleError                    an agent call would repeat a call further up the chain (also a ValueError)
├── DataStoreError                    awa's data store (runs, steps, results) cannot be opened or used
│   ├── SchemaVersionError            the schema is behind, a newer awa changed it in a breaking way, or a migration file changed
│   └── StoreConflictError            a step saved with a stale revision, or an approval decided twice
│ Tools, skills and memory
├── ToolNotFoundError
├── ToolTimeoutError                  a hardened tool call timed out (also a TimeoutError)
├── IdempotencyKeyAlreadyClaimedError run_idempotent found its key claimed (awa.tools)
├── PeopleError                       a people search, the people file or a role: recipient (awa.people, preview)
├── PluginError                       a plugin cannot be found, loaded or registered (awa.plugins)
├── PatternError                      a pattern suggestion or trial cannot be made (awa.blueprints)
├── TraceDeleteError                  trace files could not all be deleted; it names the ones left (also an OSError; awa.observability)
├── SkillNotFoundError / SkillAlreadyRegisteredError / SkillManifestError / SkillReferenceNotFoundError
├── MemoryScopeError                  the run does not identify the owner a memory scope needs
├── InvalidGateActorError             valid_actor rejected a resume payload (awa.tools.approval)
│ Data files
├── DataError                         a call to awa.data cannot be carried out: no path given, a folder without files, a cancelled run (also a ValueError)
│   ├── PathNotAllowedError           a missing path, a symbolic link, or a path outside the allowed folder
│   ├── UnsupportedTypeError          a file kind that is not accepted (awa.data.ingest with an allow-list)
│   ├── TypeMismatchError             the content does not match the file name
│   ├── RowLimitError                 a table has more rows than the caller is willing to read (awa.data row reader)
│   ├── TooLargeError                 a file, or all files together, above the size limit
│   ├── TooManyFilesError             more files than the limit
│   └── UnsafeArchiveError            a zip archive that is unsafe to unpack (path escape, link, password, bomb)
│ Build and integrate
├── BlueprintError                    a blueprint cannot be read, validated, built or designed (awa.blueprints)
├── ScaffoldError                     a project cannot be created
├── EvalError                         an invalid dataset, scorer spec or saved report
├── AdapterError                      an existing tool, graph or skill cannot be imported
├── ExportError                       an export cannot be written: a destination that is not an earlier export, an unusable name, code that does not import
│   └── ExportUnsupportedError        the blueprint uses features the export does not carry over; nothing was written
├── MCPError                          an MCP server is misconfigured, unreachable or cannot list its tools
│   └── MCPToolDriftError             a pinned server's tools changed
├── UpstreamCompatibilityError        the installed LangChain/LangGraph lacks an API awa needs (also an ImportError)
│ Serving, jobs and automation
├── AuthenticationError               missing or wrong credentials
│   └── WebhookVerificationError      a webhook signature is missing or wrong, or its timestamp is stale
├── PermissionDeniedError             the caller lacks the role an operation needs
├── InvalidRequestError               the request asks for something the system does not allow (also a ValueError)
├── RateLimitExceededError            the caller exceeded its rate limit
│   └── QuotaExceededError            a tenant's stored data, concurrent uploads or concurrent builds are at the limit (data endpoints)
├── UploadNotFoundError               no such upload for this caller (unknown, expired, or of another tenant)
├── UploadTimeoutError                an upload stalled or took too long (HTTP 408)
├── BuildNotFoundError                no such build for this caller
├── BuildConflictError                the build's status does not allow this (answers need needs_input, runs need completed)
├── BuildDesignInvalidError           a build could not produce a valid design; details list the issues
├── IdempotencyConflictError          an Idempotency-Key whose first request still runs
├── IdempotencyMismatchError          an Idempotency-Key reused with a different request
├── SystemNotFoundError / ThreadNotFoundError
├── RunNotFoundError                  no such run for this caller
│   └── NoWaitingRunError             no process run waits on the event's correlation key
├── SystemBusyError                   the system or thread is already running
├── ServiceUnavailableError           shutting down, or at the concurrency limit
├── NoPendingInterruptError           resume on a thread that waits for nothing (also a ValueError)
├── AuditChainError                   an audit file has a damaged line (also a ValueError; awa.observability)
├── JobFailedError                    a background job failed or was cancelled (awa.server.jobs)
├── WebhookDeliveryError              a webhook callback could not be delivered
├── AutomationError
│   ├── InvalidCronError              a cron expression or time zone cannot be parsed (also a ValueError)
│   ├── TemplateError                 a malformed automation template (also a ValueError)
│   ├── ProcessRunStateError          the process run's status forbids the change (HTTP 409)
│   │   └── CheckpointLostError       the paused state of a waiting run is gone (it can be started again)
│   └── WaitTimeoutError              wait_for_event or an approval timed out (also a TimeoutError)
│ Setup
├── ConfigurationError                invalid configuration where it is used (also a ValueError)
│   ├── MissingConfigurationError     a backend lacks required configuration (for example AWA_POSTGRES_DSN)
│   └── ComponentError                a component switch or replacement is not valid
├── OptionalDependencyError           a feature needs an extra that is not installed (also an ImportError)
└── PostgresSetupTimeoutError         another process held the Postgres setup lock too long (also a TimeoutError)
```

What is *not* raised: inside a team, a failed delegation (an error, a timeout, its
`delegation_limits`) comes back to the orchestrator's model as a tool result with
`status="error"`, so it can adapt. Only a `UsageLimitExceededError` of the whole run tree,
cancellation and a pause for an approval leave a delegation. Hardened tools with
`on_error="return"` likewise turn failures into error tool results.

## Error codes

Every error has a stable, machine-readable `code` (`error.code`) and a `retryable` flag. A code is
the class name without `Error`, in lower case with underscores and the prefix `awa.`; the errors of
files and uploads start with `awa.data.` and those of builds with `awa.build.` instead. Codes stay
the same across versions. The HTTP API returns the code as `error.code` in an error body
([Serving](../develop/serving.md#who-may-call-and-what-they-see)). `retryable` is `yes` when
repeating the same operation unchanged may succeed.

| Code | Class | Retryable |
| --- | --- | --- |
| `awa.adapter` | `AdapterError` | no |
| `awa.agent_already_registered` | `AgentAlreadyRegisteredError` | no |
| `awa.agent_not_found` | `AgentNotFoundError` | no |
| `awa.agent_run` | `AgentRunError` | no |
| `awa.audit_chain` | `AuditChainError` | no |
| `awa.authentication` | `AuthenticationError` | no |
| `awa.automation` | `AutomationError` | no |
| `awa.blackboard_conflict` | `BlackboardConflictError` | no |
| `awa.blueprint` | `BlueprintError` | no |
| `awa.build.conflict` | `BuildConflictError` | no |
| `awa.build.design_invalid` | `BuildDesignInvalidError` | no |
| `awa.build.not_found` | `BuildNotFoundError` | no |
| `awa.build.usage_limit` | `BuildUsageLimitError` | no |
| `awa.cache_store` | `CacheStoreError` | no |
| `awa.call_cycle` | `CallCycleError` | no |
| `awa.checkpoint_lost` | `CheckpointLostError` | no |
| `awa.communication_not_allowed` | `CommunicationNotAllowedError` | no |
| `awa.component` | `ComponentError` | no |
| `awa.configuration` | `ConfigurationError` | no |
| `awa.data` | `DataError` | no |
| `awa.data.path_not_allowed` | `PathNotAllowedError` | no |
| `awa.data.quota_exceeded` | `QuotaExceededError` | yes |
| `awa.data.row_limit` | `RowLimitError` | no |
| `awa.data.too_large` | `TooLargeError` | no |
| `awa.data.too_many_files` | `TooManyFilesError` | no |
| `awa.data.type_mismatch` | `TypeMismatchError` | no |
| `awa.data.unsupported_type` | `UnsupportedTypeError` | no |
| `awa.data.upload_not_found` | `UploadNotFoundError` | no |
| `awa.data.upload_timeout` | `UploadTimeoutError` | yes |
| `awa.data.zip_unsafe` | `UnsafeArchiveError` | no |
| `awa.data_store` | `DataStoreError` | no |
| `awa.error` | `AwaError` | no |
| `awa.eval` | `EvalError` | no |
| `awa.export` | `ExportError` | no |
| `awa.export_unsupported` | `ExportUnsupportedError` | no |
| `awa.guardrail_tripwire` | `GuardrailTripwireError` | no |
| `awa.handoff` | `HandoffError` | no |
| `awa.idempotency_conflict` | `IdempotencyConflictError` | yes |
| `awa.idempotency_key_already_claimed` | `IdempotencyKeyAlreadyClaimedError` | no |
| `awa.idempotency_mismatch` | `IdempotencyMismatchError` | no |
| `awa.invalid_cron` | `InvalidCronError` | no |
| `awa.invalid_gate_actor` | `InvalidGateActorError` | no |
| `awa.invalid_request` | `InvalidRequestError` | no |
| `awa.job_failed` | `JobFailedError` | no |
| `awa.mailbox_full` | `MailboxFullError` | yes |
| `awa.max_steps_exceeded` | `MaxStepsExceededError` | no |
| `awa.mcp` | `MCPError` | no |
| `awa.mcp_tool_drift` | `MCPToolDriftError` | no |
| `awa.memory_scope` | `MemoryScopeError` | no |
| `awa.message_request_timeout` | `MessageRequestTimeoutError` | yes |
| `awa.messaging` | `MessagingError` | no |
| `awa.missing_configuration` | `MissingConfigurationError` | no |
| `awa.missing_price` | `MissingPriceError` | no |
| `awa.model_provider` | `ModelProviderError` | no |
| `awa.model_rate_limit` | `ModelRateLimitError` | no |
| `awa.model_resolution` | `ModelResolutionError` | no |
| `awa.no_pending_interrupt` | `NoPendingInterruptError` | no |
| `awa.no_progress` | `NoProgressError` | no |
| `awa.no_waiting_run` | `NoWaitingRunError` | no |
| `awa.optional_dependency` | `OptionalDependencyError` | no |
| `awa.output_rejected` | `OutputRejectedError` | no |
| `awa.pattern` | `PatternError` | no |
| `awa.people` | `PeopleError` | no |
| `awa.permission_denied` | `PermissionDeniedError` | no |
| `awa.plugin` | `PluginError` | no |
| `awa.postgres_setup_timeout` | `PostgresSetupTimeoutError` | yes |
| `awa.process_run_state` | `ProcessRunStateError` | no |
| `awa.rate_limit_exceeded` | `RateLimitExceededError` | yes |
| `awa.recipient_not_found` | `RecipientNotFoundError` | no |
| `awa.run_cancelled` | `RunCancelledError` | no |
| `awa.run_not_found` | `RunNotFoundError` | no |
| `awa.run_timeout` | `RunTimeoutError` | yes |
| `awa.scaffold` | `ScaffoldError` | no |
| `awa.schema_version` | `SchemaVersionError` | no |
| `awa.service_unavailable` | `ServiceUnavailableError` | yes |
| `awa.skill_already_registered` | `SkillAlreadyRegisteredError` | no |
| `awa.skill_manifest` | `SkillManifestError` | no |
| `awa.skill_not_found` | `SkillNotFoundError` | no |
| `awa.skill_reference_not_found` | `SkillReferenceNotFoundError` | no |
| `awa.step_check_failed` | `StepCheckFailedError` | no |
| `awa.step_rejected` | `StepRejectedError` | no |
| `awa.store_conflict` | `StoreConflictError` | no |
| `awa.structured_output` | `StructuredOutputError` | no |
| `awa.system_busy` | `SystemBusyError` | yes |
| `awa.system_not_found` | `SystemNotFoundError` | no |
| `awa.task` | `TaskError` | no |
| `awa.task_claim` | `TaskClaimError` | no |
| `awa.task_conflict` | `TaskConflictError` | no |
| `awa.task_dependency` | `TaskDependencyError` | no |
| `awa.task_not_found` | `TaskNotFoundError` | no |
| `awa.task_permission` | `TaskPermissionError` | no |
| `awa.task_state` | `TaskStateError` | no |
| `awa.task_timeout` | `TaskTimeoutError` | yes |
| `awa.template` | `TemplateError` | no |
| `awa.thread_not_found` | `ThreadNotFoundError` | no |
| `awa.tool_not_found` | `ToolNotFoundError` | no |
| `awa.tool_timeout` | `ToolTimeoutError` | yes |
| `awa.topology` | `TopologyError` | no |
| `awa.trace_delete` | `TraceDeleteError` | no |
| `awa.upstream_compatibility` | `UpstreamCompatibilityError` | no |
| `awa.usage_limit_exceeded` | `UsageLimitExceededError` | no |
| `awa.wait_timeout` | `WaitTimeoutError` | no |
| `awa.webhook_delivery` | `WebhookDeliveryError` | yes |
| `awa.webhook_verification` | `WebhookVerificationError` | no |

::: awa.errors
