# Judge Contract

> "An agent without an exit condition stops when it feels done, which is not a
> condition, it is a mood."

Consumers of this contract: the **manager** (finish-line evaluation, build step
4+) and the **dispatch instruction** for autonomous work units. It is never
always-on worker context.

## Requirements

1. **Every autonomously dispatched work unit ships a judge**: a command that
   exits `0` (pass) or `1` (fail) **and prints a reason** either way. A silent
   exit code is not a judge — the reason is what makes failures diagnosable and
   passes auditable.
2. Judges wrap whatever verification fits: test suites, schema checks, file
   assertions, or an LLM yes/no where mechanical checks can't reach. The
   wrapper still exits 0/1 + reason.
3. **No judge → no autonomous dispatch.** The work runs as an interactive
   session the human hops into via muxplex instead. Exploratory work is
   loop-shaped; don't fake a judge for it.
4. A loop **closes** only when the judge passes. Finish lines are honest by
   construction — the judge, not the worker's mood, closes them.

## The broken-test protocol

**Judges are broken-tested before batch dispatch.** A judge that never fails is
decoration, and every green light after it is meaningless.

Before trusting a judge:

1. Run it against a **known-good** artifact → it MUST pass (exit 0, reason printed).
2. Run it against a **deliberately broken** artifact (sabotage the exact
   failure mode it guards against) → it MUST fail (exit 1, reason printed).
3. Only after both directions are verified may the judge gate autonomous
   dispatch or loop closure.

Judges that ship in this repo encode the protocol as a `--self-test` flag that
runs both directions and asserts both (see `scripts/local_roundtrip.sh`).

## Output convention

```
PASS: <one-line reason>     # exit 0
FAIL: <one-line reason>     # exit 1
```
