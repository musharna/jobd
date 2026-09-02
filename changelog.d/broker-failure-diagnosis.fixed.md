- **`job ping` and the CLI error boundary now name _which_ network failure happened.**
  DNS, connection refused, connect timeout, read timeout and TLS were all collapsed
  into a single `unreachable` string, so the two cases with opposite remedies were
  indistinguishable: a refused connection means the path is fine and the broker is
  not running, while a dropped connection means the broker is probably healthy and a
  firewall or tailnet ACL is in the way. Each now reports its own `kind` plus a hint
  pointing at the layer that can actually be fixed. The error boundary's old advice,
  "is the broker running?", was actively wrong for a dropped packet.
- **`job ping` no longer calls a broker that answered "unreachable".** A 401, a 5xx,
  or a non-ok body now render as `reachable, but not healthy` (the exit code is
  still 2).
- **The MCP server passes the same diagnosis to agents.** A transport error now
  reads `jobd transport error (timeout): ...` followed by the hint, instead of a
  bare exception string. An agent cannot check the host itself, so it is more
  dependent on that steer than a human is: from "timed out" alone the obvious
  inference is "the broker is down", which is wrong for a dropped packet. The
  JSONL call log records the specific `transport_<kind>` rather than a flat
  `transport`.
- **`job ping --json` gains `kind` and `hint`.** Its `reachable` field now means "the
  broker sent us bytes" rather than "nothing went wrong", so an authentication
  failure is no longer reported as a network failure.
