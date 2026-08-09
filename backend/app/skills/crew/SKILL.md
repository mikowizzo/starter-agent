---
name: crew
description: "Crew awareness — lets any clone discover who else is on the crew, see their roles and ports, and coordinate with them. Use when the user says 'who's on the crew', 'introduce the crew', 'ask everyone', 'check on the crew', or any question about fellow crew members."
license: MIT
---

# Crew

Crew awareness and coordination for Straw Hat clone instances.

Every clone lives on the shared `starter-app-net` Docker network with a unique
alias `backend-<name>`. This skill discovers all crew members by inspecting the
network, then presents a roster with each member's name, reachable address, and
online status.

## How it works

Run `scripts/crew.py` to get the live crew roster. It inspects the
`starter-app-net` Docker network for `backend-*` aliases, checks which ones are
reachable, and prints a formatted roster.

## Usage

```bash
python scripts/crew.py
```

Output is a table:

```
🏴‍☠️ Straw Hat Crew Roster
─────────────────────────────────────────────
Name        Address                       Status
chopper     http://backend:8000           ✅ online
nami        http://backend-nami:8000      ✅ online
robin       http://backend-robin:8000     ✅ online
─────────────────────────────────────────────
3 crew members — 3 online, 0 offline
```

## Talking to crew members

Once you know who's on the crew, use the **talk_to** tool (TeamComms) to send a
private message to any member. Pass their name and your message:

```
talk_to(name="nami", message="Hey Nami, can you check the weather forecast?")
```

Use the roster to discover crew members, then use talk_to to reach out to them.
