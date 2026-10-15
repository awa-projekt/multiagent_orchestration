# Root-Cause Debugging Checklist

A worked example of tracing a symptom to its root cause with repeated "why" questions,
stopping only when the answer names something that, if changed, prevents the symptom.

1. **Symptom**: a scheduled job silently stops processing after ~1000 items.
2. **Why?** A worker thread raised an unhandled exception and died.
3. **Why?** The exception was a `KeyError` on a response field the API sometimes omits.
4. **Why does the API sometimes omit it?** The field is only present when a downstream
   service finishes within a timeout; on slow days it is dropped.
5. **Why did that kill the whole job instead of just that item?** The per-item error
   handling only caught the exception types seen during initial development in testing,
   not `KeyError`.
6. **Root cause**: per-item error handling is an allow-list of expected exception types
   rather than a catch-all with per-item isolation, so any new failure mode escalates to
   a full job failure with no visibility into which items succeeded.

The fix that matches this root cause: make per-item failure isolated and logged
(the job continues past a bad item and records which ones failed), not a broader
`try/except Exception` around the whole job (which would hide the same root cause again
under a different symptom) and not merely adding `KeyError` to the allow-list (which
fixes this instance but not the next new exception type).

## Reusable diagnostic questions

- Does the failure happen every time, or only under specific conditions (load, timing,
  specific input shape, specific environment)? The difference between "always" and
  "sometimes" is itself a strong clue.
- Is this the first time this exact failure occurred, or has a similar one been patched
  before without addressing the underlying pattern?
- What would have to be true for the "obvious" explanation to be wrong? If nothing was
  checked to rule that out, the diagnosis is not yet confirmed.
