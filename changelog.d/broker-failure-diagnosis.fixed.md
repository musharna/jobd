`job ping` and the CLI's broker-error boundary now name **which** network failure
occurred — DNS, connection refused, connect timeout, read timeout, or TLS — and
print a hint pointing at the layer that can actually be fixed. A refused
connection says the path is fine and the broker is not running; a dropped
connection says to look at a firewall or tailnet ACL instead, because the broker
is probably healthy. Previously all five collapsed into one `unreachable` string,
which is how a healthy, freshly-upgraded broker was diagnosed as down.

`job ping` also no longer reports a broker that _answered_ as "unreachable" — a
401, a 5xx, or a non-ok body now render as "reachable, but not healthy" (exit
code is still 2). `job ping --json` gains `kind` and `hint` fields, and its
`reachable` field now means "the broker sent us bytes" rather than "nothing went
wrong".
