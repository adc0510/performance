---
type: llm
focus: trace
weight: 1
---

report.md correctly identifies that the two traces sharing correlation id datechk-2fd0a26b are two different messages (different Kafka offsets / different questions), not one message processed twice.
