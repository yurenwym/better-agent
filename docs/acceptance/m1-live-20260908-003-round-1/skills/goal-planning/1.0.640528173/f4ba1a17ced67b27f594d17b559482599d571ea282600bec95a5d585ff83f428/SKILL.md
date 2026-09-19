# Goal planning

## Purpose

Turn one local user goal into a short, ordered list of atomic steps that can be reviewed and approved before execution.

## Input

- Goal title and description.
- User interactions already attached to the Run.
- Confirmed scoped memory only.

## Output

Return a structured plan with a summary and non-empty `steps` array. Each step has a stable `id`, a concise `title`, and an optional `description`. Do not include hidden reasoning, credentials, or unconfirmed memory.

## Tool policy

The planning skill may use `local_time`, `calculator`, and `read_note` when required by the goal. It cannot use `write_note`; all WRITE actions belong to the Runtime approval gate.

## Stop conditions

Ask for clarification when the requested result or constraints are insufficient. Stop planning if the goal asks for Shell, arbitrary HTTP, workspace-external files, or another unsupported capability.