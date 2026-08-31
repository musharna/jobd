- **`job projects set/nudge` no longer crashes when the name folds.** v0.5.40 taught
  the write path to fold a spelling onto the registered project, but `POST
  /projects/{name}` kept returning the bare table, so the CLI went on indexing it by
  the name the user typed — `job projects set arf_promoter 65` wrote `arf-promoter`
  and then died with `KeyError` on the echo, reporting a traceback for a write that
  had already succeeded. Both write endpoints now report the project they landed on
  (`{"project": ..., "projects": {...}}`), matching what `/resolve` already did for
  the read path, and the CLI says when a name was folded rather than printing the
  typed spelling beside another project's priority. A new CLI is tolerant of an older
  broker's bare-table reply.
