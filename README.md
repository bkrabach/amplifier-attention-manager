# amplifier-attention-manager

An attention firewall for unattended AI work: `dispatch` launches workers into
detached `am-*` tmux sessions, a `supervise` loop observes them, a judge
command gates every finish line, and blocked workers escalate by writing
decision packets to a disk queue you answer on your schedule.

Start here: [`docs/DOGFOOD.md`](docs/DOGFOOD.md) (quickstart + full CLI flow).
Design: [`docs/designs/attention-manager.md`](docs/designs/attention-manager.md).

## Install

```bash
uv tool install 'amplifier-attention-manager[attractor] @ git+https://github.com/bkrabach/amplifier-attention-manager@main'
```

## Use from Amplifier sessions

Normal Amplifier sessions can drive the CLI well — the repo ships an
owner-side bundle with an `attention-manager` skill (dispatch flows, the
goal-derived judge pattern, redispatch-on-fail, verified footguns):

```bash
amplifier bundle add git+https://github.com/bkrabach/amplifier-attention-manager@main
```

Or compose just the behavior (`behaviors/attention-manager.yaml`) into your
own bundle. Either way, sessions load the skill on demand:
`load_skill(skill_name="attention-manager")`.

**Caveat (validated):** the skills tool's `config.skills` lists REPLACE across
composed bundles — last wins. If you compose multiple skill-shipping bundles,
merge their skill lists in your own `tool-skills` config override.

**Troubleshooting — "Not a valid bundle: missing bundle.md" (hit live):**
sessions that referenced this repo as a bundle source before the bundle
landed (commit `6829f37`) keep a stale shallow clone under
`~/.amplifier/cache/`. Refresh it:

```bash
d=$(ls -d ~/.amplifier/cache/amplifier-attention-manager-*) \
  && git -C "$d" fetch --depth 1 origin main \
  && git -C "$d" reset --hard origin/main
```

(or run `amplifier update`).
