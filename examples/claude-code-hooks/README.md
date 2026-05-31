# Claude Code hooks for jobd (optional)

These are **example** [Claude Code hooks](https://docs.claude.com/en/docs/claude-code/hooks)
that integrate jobd into an agent's workflow. They are not required to run jobd
— they're here because the agent-babysitting story is a big part of why jobd
exists. Copy the ones you want into your `~/.claude/` setup and wire them up in
`settings.json`.

| Hook                | Type                       | What it does                                                                                                                                                                                                                                                            |
| ------------------- | -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `jobd-nudge.sh`     | PreToolUse(Bash), advisory | Detects a heavy command (training, R pipeline, `accelerate launch`, …) and prints a non-blocking nudge to submit it through jobd instead. Never blocks.                                                                                                                 |
| `jobd-block-gpu.sh` | PreToolUse(Bash), blocking | Hard-blocks GPU launches that target a specific GPU host and bypass jobd (exit 2). Supports `# NO_GPU` / `# CONCURRENT_OK` / `# VRAM=NGB` override markers, each audit-logged. **No-op until you set `JOBD_GPU_SSH` and `JOBD_GPU_HOST_PAT`** (see the header comment). |

`test-jobd-nudge.sh` is a small self-contained test harness for `jobd-nudge.sh`.

## Wiring example (`~/.claude/settings.json`)

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/jobd-nudge.sh"
          },
          {
            "type": "command",
            "command": "bash ~/.claude/hooks/jobd-block-gpu.sh"
          }
        ]
      }
    ]
  }
}
```

Both hooks emit broker `/events` telemetry when `JOBD_URL` is set, so hook
activity shows up in the same observability stream as job lifecycle events.
