- **A starting worker killed the running jobs of every other jobd worker under
  the same user.** The startup sweep exists to kill workloads a crashed
  predecessor left behind, but a scope was named `jobd-<id>.scope` and the sweep
  took every such unit in the session as its own. Starting a second worker for a
  different broker (an isolated test broker, the `JOBD_LIVE` suite) SIGKILLed
  the production worker's jobs, and two brokers issuing the same job id collided
  on one unit name. Scopes are now `jobd-<ns>-<id>.scope`, where `<ns>` is
  derived from the broker URL, and the sweep is limited to its own `<ns>`.
  Scopes it leaves alone are named in a warning. One consequence on upgrade: a
  scope left behind by a worker older than this release has no namespace and is
  no longer swept; the warning names it and gives the command to stop it. A
  normal restart drains first, so this only matters after a crash.
  `systemctl --user status jobd-<id>.scope` becomes `jobd-<ns>-<id>.scope`; the
  worker logs the unit name when it starts a job.
