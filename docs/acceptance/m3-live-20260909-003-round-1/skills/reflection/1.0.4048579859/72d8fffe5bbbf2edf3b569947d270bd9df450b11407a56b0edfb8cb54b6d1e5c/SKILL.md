# Execution reflection

## Purpose

Review the completed Run and propose evidence-backed preference or habit candidates for user confirmation.

## Input

- Goal and approved PlanVersion.
- Structured events and tool summaries.
- Confirmed memory context.

## Output

Return zero or more candidates with `kind` (`preference` or `habit`), `content`, `scope`, `confidence`, and evidence event IDs. A candidate is not a confirmed fact and must never be written directly to long-term memory.

## Tool policy

Reflection is read-only. It may inspect the event summaries supplied by Runtime but cannot call WRITE tools, alter the plan, change state, or edit Markdown.

## Stop conditions

Return no candidate when there is no explicit or repeated evidence. Never copy credentials, hidden reasoning, arbitrary user text, or unsupported conclusions into a candidate.