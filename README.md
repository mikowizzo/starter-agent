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

## Secure remote access with Tailscale (recommended)

By default, the agent only listens on `localhost` — nobody else can reach it.
To access it from your phone, laptop, or other devices **without exposing it to
the public internet**, use [Tailscale](https://tailscale.com):

**1. Install Tailscale** on this machine and any device you want to access from.

**2. Find your machine's Tailscale IP** (starts with `100.x.y.z`):

```bash
tailscale ip -4
```

**3. Set the bind host** in your `.env`:

```bash
BIND_HOST=100.x.y.z    # your Tailscale IP from step 2
```

**4. Restart and open** `http://100.x.y.z:3000` from any device on your tailnet.

The agent binds only to your Tailscale interface — it is invisible to the public
internet. For more, see the [Tailscale getting started guide](https://tailscale.com/kb/1017/install).

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
