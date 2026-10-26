---
type: llm
focus: trace
weight: 1
---

report.md attributes find_law_citations' time to waiting on the database query rather than Python work, and proposes a database-side fix (query shape, index, batching or caching) pointing at app/repositories/law_repository.py.
