---
tags: [skill-creator]
max_turns: 40
timeout_seconds: 1200
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

sqlparse is super slow for us: sql_work took 146 seconds according to this trace /home/thang/code/performance/examples/out/realcode/sqlparse.json (sqlparse was traced as project code, with TRACE_LOCALS on). Which sqlparse functions should we optimize first? Write your recommendation to report.md.
