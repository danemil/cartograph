"""The memory capability: observations an agent wrote down, and how to find them.

Separate from the graph in every sense that matters — its own SQLite file, its
own schema version, its own write path — but not in the sense an agent sees. It
speaks the same capability envelope, pages with the same cursors, and reports
paths relative to the same repository root, because a second protocol would be
a second thing for an agent to learn and a second thing to get wrong.

`store` owns the schema and the queries; `vector` is the optional sqlite-vec
seam; `cli` builds the `carto mem` parser and shapes results for the shared
emit path in `cartograph.cli`.
"""
