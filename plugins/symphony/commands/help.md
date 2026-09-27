---
description: Show Claude Code commands for Symphony
---

# Symphony commands

- `/symphony:enable` — enable automatic governance for this project.
- `/symphony:start <task>` — run one managed task without enabling the project.
- `/symphony:bypass <task>` — run one task outside Symphony without changing enablement or an active run.
- `/symphony:disable` — gracefully stop active work, archive recovery context, and disable future activation.
- `/symphony:status` — show enablement, hook activation, route, lead, and compact delegations.
- `/symphony:agents [--all]` — show current latest delegation records; `--all` includes retained history.
- `/symphony:reassess` — reassess subsequent work at the current evidence boundary.
- `/symphony:proceed` — accept a route your plan clamps to a weaker model, for this session.
- `/symphony:boost [xhigh|max|off]` — boost subsequent assessors in this session; status shows requested and effective effort.
- `/symphony:stop [--force]` — request a safe stop; `--force` acknowledges interruption of active work.
- `/symphony:version` — show which Symphony build is running this session, and whether a newer one is installed.
- `/symphony:help` — show this help.

SYMPHONY_CONTROL: help
