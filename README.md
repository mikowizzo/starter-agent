# Starter Agent

A personal AI assistant you can run on your own machine. Powered by [OpenCode](https://opencode.ai), built with agno, FastAPI, React, and Docker.

## Quick Start

**1. Clone the repo**

```bash
git clone https://github.com/mikowizzo/starter-agent.git
cd starter-agent
```

**2. Add your API key**

```bash
cp .env.example .env
```

Open `.env` and paste your OpenCode API key:

```
OPENCODE_API_KEY=your-key-here
```

**3. Build with your host's Docker GID**

The agent can clone itself via Docker, so the container needs access to the
host's Docker socket. The socket is owned by a `docker` group whose GID varies
by host. Build with your host's GID so the in-container user can reach it:

```bash
export DOCKER_GID=$(getent group docker | cut -d: -f3)
docker compose up --build
```

(If you don't care about self-cloning, you can skip the `DOCKER_GID` export —
the default works on most Debian/Ubuntu hosts.)

**4. Open it**

Open **http://localhost:3000** in your browser. That's it.

---

## Make it yours

| Want to change... | Open this file... |
|---|---|
| Agent personality | `backend/app/agents/coordinator.py` |
| AI model | `backend/app/models.py` |
| Tools | `backend/app/tools/code_tools.py` |
| Add skills | Drop a folder with `SKILL.md` in `backend/app/skills/` |
| Self-cloning | `backend/app/tools/clone_tools.py` — the agent can spawn independent copies of itself |

## Built with

agno · FastAPI · React · TypeScript · Vite · Tailwind CSS · SQLite · Docker

## License

MIT
