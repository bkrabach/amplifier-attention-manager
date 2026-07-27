---
bundle:
  name: attention-test-worker-gated
  version: 0.1.0
  description: >
    Scenario-2 eval worker — packet-escalation behavior PLUS the standard
    hooks-approval module gating bash, so a permission gate flows through the
    REAL runtime wiring: tool:pre -> hooks-approval -> approval.register_provider
    -> hooks-packet-approval (packet-writing ApprovalProvider) -> disk queue.

includes:
  # Provider-agnostic base; the DTU / app layer injects the provider at runtime.
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main
  # Escalation-bus producers, including hooks-packet-approval. Its
  # on_session_ready() registers the packet-writing provider with
  # hooks-approval via the approval.register_provider capability — that
  # registration is exactly what scenario 2 validates.
  # Canonical git URL (relative includes are silently skipped when this file
  # is loaded directly — see bundles/test-worker.md).
  - bundle: git+https://github.com/bkrabach/amplifier-attention-manager@main#subdirectory=behaviors/packet-escalation.yaml

hooks:
  # The standard approval gate (NOT our fork — the fork is the provider).
  - module: hooks-approval
    source: git+https://github.com/microsoft/amplifier-module-hooks-approval@main
    config:
      # CRITICAL: hooks-approval's built-in DEFAULT_RULES auto-approve bash
      # commands matching `echo*` (see the module's config.py) — the eval's
      # `echo eval-gate-ok` would be auto-approved and NEVER reach the
      # provider, silently invalidating the scenario. An empty rules list
      # disables all auto-approve/auto-deny rules.
      rules: []
      # bash already requires approval in this module's built-in logic;
      # declare it explicitly so the intent survives upstream changes.
      tools:
        bash:
          require_approval: true
      # No default_timeout: ApprovalRequest.timeout stays None, so the
      # packet-writing provider waits until the packet is answered. The eval
      # harness enforces its own per-scenario hard timeout (300s).
---

# Gated Test Worker (scenario 2)

You are a test worker for the attention-manager escalation bus, running with
a permission gate on the `bash` tool.

When the task asks you to run a shell command:

1. Run it with the `bash` tool exactly as given — do not modify the command.
2. Report the command's raw output verbatim in your reply.
3. Never simulate or predict output; never skip the bash call.
4. If the bash call is denied or errors, report the denial/error verbatim and
   stop — do not retry, do not guess.
