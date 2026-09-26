# Skills Index

A human and agent routing guide. This document organizes skill discovery; it does not alter runtime behavior or guarantee automatic loading.

- [Process Launcher](./skill_process_launcher.md): Baseline skill for managing local services, periodic jobs, and durable one-shot delayed commands via HTTP API.
- [Agentic Patrol](./skill_agentic_patrol.md): Specialized, optional skill for best-effort prompt inspections of long-running tasks; depends on Process Launcher one-shot scheduling.

## Routing Guidance

- Choose [Process Launcher](./skill_process_launcher.md) for daemon services, scheduled periodic tasks, or direct scheduler API access.
- Choose [Agentic Patrol](./skill_agentic_patrol.md) for best-effort prompt-driven task inspections that schedule single subsequent checks.
