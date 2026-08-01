# Attention Manager — capability awareness

This environment can drive the **attention-manager** CLI: dispatch detached,
judge-gated workers into `am-*` tmux sessions, supervise them, and answer
escalation packets from a disk queue — walk-away autonomy for real work units.

**Before driving the CLI in any way, ALWAYS load the skill first:**

```
load_skill(skill_name="attention-manager")
```

The skill carries the verified commands, the goal-derived judge pattern, the
redispatch-on-fail loop, and the footguns (judge timeouts, bundle-ref rules,
escalation prerequisites). Driving the CLI from memory instead of the skill
is how workers get dispatched without judges or escalation paths.

**When to use:** you are leaving work running unattended, running parallel
work units, or "done" must be provable with real evidence. If you'll stay
attending the session and verify results yourself, an in-session goal loop
is simpler — skip AM.

**Prerequisite:** `attention-manager` on PATH
(`uv tool install 'git+https://github.com/bkrabach/amplifier-attention-manager@main'`),
plus `tmux` and a working `amplifier` CLI.
