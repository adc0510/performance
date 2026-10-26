---
tags: [skill-creator]
max_turns: 40
timeout_seconds: 1200
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

order 1 in scenario_business (locals_stress.py) comes back with status "partial" and total 22 and I don't get why. Using the perf trace a_business.json, explain exactly which order lines had problems and why, what got reserved, and how the total of 22 is computed step by step. Write it to explanation.md.

Input files: a_business.json, locals_stress.py
