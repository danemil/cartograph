---
name: build-graph
description: Build or refresh the Cartograph code graph and check that it is current. Use when carto reports no graph or stale results, or on a fresh checkout. Not for querying the graph.
---

## Build the graph

Every other `carto` skill needs a graph. This one creates and refreshes it.

### When to use this
- A `carto` command exited `2` with `remediation: "carto build"`.
- You have pulled or edited a lot of code and results look stale.
- First time working in this checkout.

### When NOT to use this
- The graph exists and is current → just run the query you actually wanted.
- Building is not free. Do not rebuild "to be safe" before every query.

### Steps

1. **Check first — do not build blindly.**
   ```
   carto status --format json
   ```
   Read `data.stale`. If the graph exists and is not stale, stop here.

2. **No graph → build it.** Minutes on a large repo; it parses every file.
   ```
   carto build --repo .
   ```

3. **Stale graph → update it, do not rebuild.** Incremental and much cheaper.
   ```
   carto update --base HEAD~1
   ```

4. **Confirm**: re-run step 1 and check `data.stale` is false.

### If it fails

- Exit `2` is a precondition, and `error.remediation` names the command that
  fixes it. Run that, then retry the original call — do not give up on the
  graph and start reading files by hand.
- Exit `1` means the call was malformed. Check the flags with
  `carto capabilities --command build`.

### Reference

`carto capabilities --format json` lists every command.
