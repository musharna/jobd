- **A registered project no longer loses its priority to how the name was typed.**
  Project names are free text chosen at submit time and were matched with a bare
  `name in projects`, so `ARFDSynInt` ran at `_default` 40 while `arfdsynint` sat
  deliberately registered at 65 — a difference of case alone, raising no error and
  visible only in a warning nothing consumed. Case and `-`/`_` are now folded when
  matching, the resolved name is what `/submit` stores and `/resolve` previews, and an
  exact hit always wins. Folding is deliberately **not** fuzzy: `phelipanche` is not
  folded onto `phelipanche-fm`, because a suffix difference is a registration decision,
  and guessing there would run work at another project's priority — the same bug
  inverted. Two registered names that fold together are reported at load and fall back
  to exact matching rather than one being picked arbitrarily.
