# Execution Liveness And Stale-State Recovery

## Why this exists

Brad had a real production failure mode where the GUI showed an execution as
`running` even though the worker was no longer doing work. The root problem was
not that the UI was broken; it was that the database still contained a
`status='running'` execution row after the worker disappeared.

The evidence from `brad-vm` was:

- the worker log stream for `DEV-3170` stopped mid-implementation
- a fresh worker process started later
- the database row remained in `implementing`
- kernel logs did **not** show a confirmed OOM-kill signature

That means "stale running state after worker interruption" is the defensible
diagnosis. "OOM" remains possible, but it was not proven by the available logs.

## Reliability layers

Brad now uses four layers, each solving a different failure mode.

### 1. Startup reconciliation

On worker startup, Brad scans for stale `running` executions and marks them
failed. This is the recovery net for:

- crashes
- hard kills
- VM reboots
- abrupt service restarts

This keeps the dashboard from showing phantom work indefinitely.

### 2. Graceful shutdown marking

Brad installs `SIGTERM` / `SIGINT` handlers. If systemd or an operator stops
the worker while an execution is active, Brad marks the execution failed before
exiting.

This specifically improves the controlled-restart path, where the worker is
asked to stop rather than simply disappearing.

### 3. Heartbeat + progress timestamps

Each active execution now stores:

- `last_progress_at`
- `last_heartbeat_at`
- `worker_id`
- `worker_pid`

The worker updates progress timestamps at meaningful workflow points and also
emits a background heartbeat while an execution is active.

This solves two separate problems:

- **progress visibility**: "Brad is still moving through work"
- **liveness visibility**: "the worker process is still alive enough to report in"

The GUI only treats a `running` execution as live if its liveness timestamps are
fresh.

### 4. Memory instrumentation

The heartbeat records:

- current RSS
- peak RSS seen by the worker process

This does not prevent failures, but it gives real forensic data the next time
someone suspects an OOM condition. That is better than inferring OOM from stale
UI state alone.

## Why heartbeat is useful even without proven OOM

Heartbeat is not an "OOM detector". It is a liveness detector.

It helps answer:

- Is the worker still checking in?
- Did the worker disappear?
- Has the execution stopped making progress?

That distinction matters because stale UI state can come from several causes:

- hard crash
- service restart
- VM restart
- hung process
- operator interruption

Heartbeat narrows the diagnosis from "something is wrong" to "this execution is
fresh" vs "this execution is stale".

## Why not rely on heartbeat alone

Heartbeat is not enough by itself:

- if the worker is cleanly stopped, graceful shutdown should fail the execution immediately
- if the worker was previously interrupted, startup reconciliation must still repair old rows

So the design is intentionally layered rather than heartbeat-only.
