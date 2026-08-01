---
bundle:
  name: attention-manager
  version: 0.1.0
  description: Owner-side bundle — a normal Amplifier session that can drive the attention-manager CLI (dispatch judge-gated workers, answer escalation packets)

includes:
  - bundle: git+https://github.com/microsoft/amplifier-foundation@main
  - bundle: attention-manager:behaviors/attention-manager
---

# Attention Manager

@attention-manager:context/attention-manager-awareness.md

---

@foundation:context/shared/common-system-base.md
