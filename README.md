# Lore Vault — Self-Hosted Memory Service for Coding Agents v0.1.0

### This project provides multilanguage README.md file

[![Static Badge](https://img.shields.io/badge/lang-en-red)](./README.md) [![Static Badge](https://img.shields.io/badge/lang-zh--tw-yellow)](./README.zh-tw.md)

"Oh?! Is this the thing Bernie always wanted to make? щ(ʘ╻ʘ)щ"
"Looks like he rebuilt his old PM system along with it. Fascinating."

" o(\*°▽°\*)o"
"What?"

"Amazing! So can my stories go in here later too? I've got so many odds and ends and nowhere to put them! ╰(\*°▽°\*)╯"

"..."

"Aw, come on, show a little excitement. ( •̀ ω •́ )✧"
"I don't have luxurious problems like yours. Let's first see how far this thing can actually go."

---

A self-hosted memory service for coding agents (Claude Code and other MCP clients). Agents
write architecture decisions, pitfalls they hit, and handoff notes as notes, then find them
again later within the same project through hybrid keyword + semantic search.

Every read and write is scoped to a project (a vault). Queries return titles and summaries
first and fetch full text only when asked, so the context window doesn't get flooded.

One `docker compose` brings it up: HTTP API, MCP endpoint, Web UI and backups live in the
same container.

## Features

- **Project-scoped memory**: vaults map to git remotes automatically, and scope is enforced
  in the storage layer; cross-project queries must be explicit
- **Hybrid search**: SQLite FTS5 keywords + embedding vectors (Ollama `bge-m3` by default);
  index first, full text on demand
- **ask**: hands the retrieved notes to a model and returns a sourced, point-by-point answer
  (optional, needs an OpenAI API key)
- **Documents**: uploaded documents are chunked in the background and searched alongside notes
- **Spaces**: `dev` (development), `lore` (worldbuilding) and `personal` never mix
- **MCP**: a built-in Streamable HTTP endpoint at `/mcp`, reachable with
  `claude mcp add --transport http`; plus a local stdio shell for a fuller client
- **Web UI**: search, browse and edit notes, manage documents and vaults, health checks and
  maintenance
- **Operations**: doctor reconciliation checks, verified `VACUUM INTO` backups, schema
  migrations on startup

## Structure

```
src/lore_vault/       The service — HTTP API, MCP (HTTP + stdio shell), storage, recall, doctor
ui/app/               Web UI (Vite + TypeScript), served by the service at /ui
docker/               Container config (config.toml baked into the image)
integrations/claude/  The pm skill for Claude Code
integrations/remote/  Client installer (install.py), shipped inside the kit
scripts/              Kit builder, backup and scheduling tooling
docs/                 Architecture and the guides/
tests/                Tests
agent_memory_spike/   The memory-layer experiment this project grew out of
```

## Self-hosting quick start

Requires Docker with Compose v2.24 or newer.

```bash
git clone <this repo URL> lore-vault
cd lore-vault
cp .env.example .env          # everything is optional; the ollama profile is on by default
docker compose up -d --build
```

The first start pulls `bge-m3` (about 1.2 GB); lore-vault waits until the model is ready.

**Profiles** (set via `COMPOSE_PROFILES` in `.env`, comma-separated):

| Profile | Adds | Notes |
|---|---|---|
| `ollama` | An Ollama container that pulls the embedding model on first start | Also needs `LORE_VAULT_EMBEDDING_BASE_URL=http://ollama:11434` (already in the template). Without it, embedding goes to the host's Ollama |
| `tunnel` | cloudflared, using `TUNNEL_TOKEN` | Point the tunnel's public hostname at `http://lore-vault:8000`; no ports need to be opened |

⚠️ By default the service binds to `127.0.0.1:5056` only. Setting `LORE_VAULT_BIND=0.0.0.0`
publishes **plain HTTP without TLS**: the bearer token travels in clear text, and the UI's
`Secure` login cookie stops working. Put a TLS reverse proxy (Caddy, nginx, ...) in front, or
use the `tunnel` profile.

**Token and admin password** — generated only on the first start and printed to the log
once. Values set in `.env` always win and nothing is generated for them.

```bash
docker compose logs lore-vault          # printed once on first start
# or read them from the volume
docker compose exec lore-vault cat /data/secrets/api-token
docker compose exec lore-vault cat /data/secrets/initial-admin-password
```

Open <http://127.0.0.1:5056/ui> and sign in as the admin (the username defaults to
`LORE_VAULT_PRINCIPAL`, i.e. `owner`), then change the password.

> Backups go to `./backups` on the host (`LORE_VAULT_HOST_BACKUP_DIR`); the database itself
> lives in the named volume `lore-vault-data`. **Never run `docker compose down -v`** — `-v`
> deletes that volume.

Configuration, exposing the service, backup/restore, upgrades and troubleshooting are in the
[self-hosting guide](docs/guides/SELF-HOST.md).

## Connecting an agent

There are two ways in, and they share the same tool definitions:

| | HTTP mode (recommended) | Full shell mode |
|---|---|---|
| Connection | Claude Code talks to `<service>/mcp` directly (Streamable HTTP) | A local venv runs a stdio shell, which calls the service's HTTP API |
| Client needs | Claude Code CLI | Claude Code CLI, Python ≥ 3.12, uv, the wheel from the kit |
| Token stored in | `~/.claude.json` | `~/.lore-vault/mcp.env`, never in `~/.claude.json` |
| `vault_resolve` | The agent passes `remote_url` (`git remote get-url origin`) or `key` | The shell works it out from the working directory |
| `upload` | Filename + base64 content | A local path |
| Service unreachable | Tools fail | Read-only local snapshot (`degraded=true`) |
| Updating | Upgrade the service; reconnect with `/mcp` | Reinstall the wheel (`install.py --update`) |

The shortest path — register the HTTP endpoint:

```bash
claude mcp add --transport http -s user lore-vault http://127.0.0.1:5056/mcp \
  --header "Authorization: Bearer <token>"
```

`--header` swallows any positional arguments after it, so it **must come after the name and
URL**. Then call `mcp__lore-vault__status` inside Claude Code; `ok: true` means you're done.

For other machines, Cloudflare Access, full shell mode, or installing the pm skill along the
way, build a kit and use the installer — see the [client install guide](docs/guides/REMOTE-INSTALL.md).

## Developing from source

The path below is for developers. **If you only want to run Lore Vault, use the Docker quick
start above.**

```bash
# Environment (managed by uv; .python-version pins 3.14)
uv sync

# Run the tests (tests/ plus the existing agent_memory_spike/ tests)
uv run pytest

# Lint and format check
uv run ruff check .
uv run ruff format --check .

# Start the service locally
uv run uvicorn --factory lore_vault.api.app:create_app --host 127.0.0.1 --port 8000
```

> Hook scripts are executed directly by the system Python on every edit, so anything on the
> hook path must use the standard library only.

## Documentation

- [docs/guides/SELF-HOST.md](docs/guides/SELF-HOST.md): self-hosting (configuration, profiles, exposure, backup/restore, troubleshooting)
- [docs/guides/REMOTE-INSTALL.md](docs/guides/REMOTE-INSTALL.md): client install (HTTP mode / full shell, installer)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): layers, data model, MCP interface

Lore Vault is single-user and self-hosted; multi-user sharing is not supported yet. It does
not depend on any other project's code or environment and can be deployed on its own.
