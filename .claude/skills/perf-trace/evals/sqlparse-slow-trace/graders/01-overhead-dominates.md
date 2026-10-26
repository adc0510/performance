---
type: llm
focus: trace
weight: 1
---

report.md states that most (roughly 90% or more) of the 146 s is perf's own recording overhead (TRACE_LOCALS and value capture), not sqlparse's own work.
