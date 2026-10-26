---
tags: [skill-creator]
max_turns: 40
timeout_seconds: 1200
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

I sent a qa message (correlation id datechk-2fd0a26b) through our retrieval-service and it took around 19 seconds, way too slow. perf is on and the traces are in /home/thang/Work/retrieval-service/perf_traces, the service code is in /home/thang/Work/retrieval-service. Figure out what is actually slow and propose concrete fixes in the code (file + what to change). Don't edit the service code, just write your findings to report.md.
