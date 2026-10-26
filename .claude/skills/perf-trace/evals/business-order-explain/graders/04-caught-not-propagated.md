---
type: llm
focus: trace
weight: 1
---

explanation.md states these OutOfStock exceptions were caught inside process_order (they did not propagate) and became warnings, which is why status is partial.
