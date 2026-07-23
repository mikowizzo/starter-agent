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

**3. Run it**

```bash
docker compose up --build
```

Open **http://localhost:3000** in your browser. That's it.

---

## Make it yours

| Want to change... | Open this file... |
|---|---|
| Agent personality | `backend/app/agents/coordinator.py` |
| AI model | `backend/app/models.py` |
| Tools | `backend/app/tools/code_tools.py` |
| Add skills | Drop a folder with `SKILL.md` in `backend/app/skills/` |

## Built with

agno · FastAPI · React · TypeScript · Vite · Tailwind CSS · SQLite · Docker

## License

MIT
