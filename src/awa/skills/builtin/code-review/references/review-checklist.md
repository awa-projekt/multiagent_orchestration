# Code Review Checklist

Work through the sections that apply to the change. Skip an item deliberately, not by
omission.

## Correctness

- Boundary conditions: empty input, a single element, the largest expected size, the
  smallest and largest valid values.
- Off-by-one errors in ranges, slices, and loop bounds.
- Handling of missing, null, or malformed fields read from external input.
- Error paths: does a failure leave the system consistent, or partially updated?
- Resource handling: are files, connections, locks, and tasks released on every path?

## Concurrency and state

- Shared mutable state accessed by more than one caller, thread, or task.
- Idempotency: does running the operation twice with the same input duplicate an effect?
- Ordering assumptions that nothing in the code actually guarantees.

## Interfaces and callers

- Every call site of a changed public function, class, or schema, not only those the new
  tests cover.
- Backward compatibility of changed schemas or stored formats that older data or other
  services still read.
- Error types and messages that callers match on.

## Tests

- A test exists for the new behavior and fails without the change.
- Boundary conditions and failure paths are covered, not only the main path.
- Tests assert observable behavior rather than internal call sequences.

## Security

- External input reaching a shell command, file path, query, or template without
  validation or parameterisation.
- Authorization checked for the specific action, not only authentication.
- Secrets, credentials, or personal data appearing in logs, errors, or prompts.

## Maintainability

- Duplicated logic that should use an existing helper.
- Dead code, unused parameters, or speculative generality introduced by the change.
- Names and comments that still describe what the code does after the change.
