# Downloader Bot

A Discord bot that bundles every image (or other allowed media) in a channel into a single zip and hands the user a download link. The zip is streamed straight into Azure Blob Storage and shared as a SAS URL — TTL defaults to 24 hours and is configurable per-guild. The end-to-end pipeline (`channel.history()` → aiohttp chunked GET → `stream-zip` → Azure block-blob upload) never materialises the full archive in memory, so it scales to channels of any size.

The bot itself only enqueues Taskiq tasks — a separate Taskiq worker (same image, different `CMD`) pulls them off RabbitMQ and runs the channel-history walk, zipping, upload, and delivery. This lets downloads outlive Discord's 15-minute interaction-token window and keeps long-running downloads in one channel from blocking another.

## Commands

- **`/download [only_me]`** — Queues a job that collects every attachment in the current channel matching the guild's `allowed_media_types` filter (defaults to all attachments), zips them, and delivers a download link. Set `only_me: true` to force private DM delivery (overrides the server's configured mode).
- **`/setup set | show | clear`** — Server-owner only.
  - `/setup set <delivery_mode> [results_channel] [retention_hours]` overwrites delivery settings in one shot. `delivery_mode=dm` sends to the requester; `delivery_mode=channel` posts in `results_channel` (required for that mode) and falls back to DM if the channel is unusable at delivery time. `retention_hours` controls SAS URL lifetime (default `24`).
  - `/setup show` prints this guild's current effective settings.
  - `/setup clear` resets `delivery_mode` to `dm` and unsets the results channel. `retention_hours` and the (currently-unused) media-type / size-cap fields are preserved.
- **`/invite`** — DMs the requester the bot's invite link (configured via `INVITE_LINK`); falls back to an ephemeral channel reply if DMs are blocked.
- **`<PREFIX>sync global|guild`** — Bot-owner only, prefix-only. Re-registers slash commands. Run this after deploying new commands.

All `/` commands are hybrid — they work with the configured `PREFIX` (e.g. `??download`) as well as the slash-command UI.

## Quick Start (Development)

The dev environment uses Docker Compose to run the bot, a Taskiq worker + scheduler, RabbitMQ (job broker), Redis (result backend + idempotency state), Postgres (per-guild settings), [Azurite](https://github.com/Azure/Azurite) (Microsoft's local Azure Blob Storage emulator), and the [Taskiq Admin](https://github.com/taskiq-python/taskiq-admin) UI on `http://localhost:3000`. No real Azure account is needed.

```bash
cp .env.example .env
# Fill in TOKEN, RABBITMQ_DEFAULT_USER/PASS, POSTGRES_PASSWORD, and TASKIQ_ADMIN_API_TOKEN.
# For Azurite, set:
#   ENVIRONMENT=dev
#   AZURE_INT_URL=http://azurite:10000/devstoreaccount1
#   AZURE_EXT_URL=http://localhost:10000/devstoreaccount1
#   AZURE_CONN_STR=DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://azurite:10000/devstoreaccount1;
# REDIS_URL and POSTGRES_DSN already point at the compose services.

docker compose up --build -d
```

The bot runs under [watchfiles](https://watchfiles.helpmanual.io/) and the worker / scheduler use Taskiq's own `--reload` flag, so any `.py` change under [downloader_bot/](downloader_bot/) triggers an automatic restart. The repo is mounted into `/bot/` inside every service.

When `ENVIRONMENT=dev`, generated SAS URLs are rewritten from `AZURE_INT_URL` (the in-network Azurite hostname) to `AZURE_EXT_URL` (the host-reachable one) so links opened in your browser actually resolve — see [downloader_bot/storage/azure.py](downloader_bot/storage/azure.py).

## Production

Build the image and run three containers from it (bot + worker + scheduler), pointing them at the same RabbitMQ, Redis, Postgres, and Azure Storage. [docker-compose.prod.yml](docker-compose.prod.yml) is a working reference.

```bash
# x86_64
docker build -t downloader-bot:<VERSION> .

# Raspberry Pi / ARM64
docker build --platform linux/arm64/v8 -t downloader-bot:<VERSION>-arm64-v8 .

# Bot (gateway-connected)
docker run -d --name downloader-bot --env-file .env.prod downloader-bot:<VERSION>

# Worker (REST-only, runs the actual downloads)
docker run -d --name downloader-bot-worker --env-file .env.prod \
  -e TASKIQ_PROCESS_ROLE=worker \
  downloader-bot:<VERSION> \
  python -m taskiq worker downloader_bot.tq:broker downloader_bot.tasks

# Scheduler (cron source for future scheduled tasks; safe to omit if nothing is scheduled yet)
docker run -d --name downloader-bot-scheduler --env-file .env.prod \
  -e TASKIQ_PROCESS_ROLE=scheduler \
  downloader-bot:<VERSION> \
  python -m taskiq scheduler downloader_bot.tq:scheduler downloader_bot.tasks
```

You'll also need RabbitMQ, Redis, and Postgres reachable from every container (managed services or self-hosted; the dev compose file shows the minimum config). The Postgres schema is bootstrapped idempotently the first time the worker opens its pool — no separate migration step required.

The production image:

- Runs as a non-root `discordbot` user
- Uses [Tini](https://github.com/krallin/tini) as PID 1 for proper signal handling and zombie reaping
- Defines per-service Docker `HEALTHCHECK`s in [docker-compose.prod.yml](docker-compose.prod.yml) — the bot uses [discordhealthcheck](https://github.com/psidex/DiscordHealthcheck) to verify its gateway connection, and the worker / scheduler use `python -m downloader_bot.worker.healthcheck` to verify a fresh Redis heartbeat sentinel exists for their `TASKIQ_PROCESS_ROLE`

For production, set `ENVIRONMENT=prod` (disables the SAS URL rewrite) and point `AZURE_CONN_STR` at your real storage account.

## Configuration

All configuration is loaded from `.env` by [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) (see [downloader_bot/config.py](downloader_bot/config.py)). Copy `.env.example` and fill in the values.

| Variable                                | Required | Description                                                                                                                                          |
| --------------------------------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `TOKEN`                                 | yes      | Discord bot token.                                                                                                                                   |
| `PREFIX`                                | yes      | Prefix for text commands (e.g. `??`). Slash commands always work regardless.                                                                         |
| `ENVIRONMENT`                           | yes      | `prod` or `dev`. Toggles the SAS URL hostname rewrite.                                                                                               |
| `ALLOWED_MEDIA_TYPES`                   | yes      | JSON array of MIME types to collect (see `.env.example`). Used as the default when a guild hasn't set its own filter.                                |
| `STORAGE_BACKEND`                       | yes      | Object-storage backend. Currently only `azure` is supported; the storage layer dispatches on this value.                                             |
| `AZURE_CONN_STR`                        | yes      | Azure Blob Storage connection string. SAS URL generation requires this to contain an account key.                                                    |
| `AZURE_CONTAINER`                       | yes      | Blob container name. The dev compose stack auto-creates one called `media`.                                                                          |
| `POSTGRES_DSN`                          | yes      | asyncpg DSN for the Postgres instance holding per-guild settings. Defaults to the compose-stack value.                                               |
| `REDIS_URL`                             | yes      | Redis URL — Taskiq result backend, `taskiq-cancellation` state holder, and app-level idempotency state. Defaults to the compose-stack value.         |
| `RABBITMQ_DEFAULT_USER`                 | yes      | RabbitMQ username. The Taskiq broker connects to `amqp://$RABBITMQ_DEFAULT_USER:$RABBITMQ_DEFAULT_PASS@rabbitmq:5672`.                               |
| `RABBITMQ_DEFAULT_PASS`                 | yes      | RabbitMQ password.                                                                                                                                   |
| `POSTGRES_USER` / `PASSWORD` / `DB`     | dev only | Used by the `postgres` compose service to bootstrap the database.                                                                                    |
| `AZURE_INT_URL`                         | dev only | Internal Azure Storage URL — the hostname the worker uses to reach the storage backend (e.g. `http://azurite:10000/devstoreaccount1`).               |
| `AZURE_EXT_URL`                         | dev only | External Azure Storage URL — the hostname end users will use to download from generated SAS URLs.                                                    |
| `LOGGING_LEVEL`                         | no       | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Defaults to `INFO`.                                                                                             |
| `INVITE_LINK`                           | no       | Bot invite URL surfaced by `/invite`. If unset, `/invite` will DM a broken link — set this to a real OAuth invite URL.                               |
| `ATTACHMENT_CHUNK_SIZE`                 | no       | CDN read chunk size for the streaming-zip pipeline (default 64 KiB). Also caps per-job in-flight bytes from the CDN.                                 |
| `TASKIQ_ADMIN_URL`                      | no       | Base URL of the Taskiq Admin UI. Required if you want the admin middleware to publish task lifecycle events.                                         |
| `TASKIQ_ADMIN_API_TOKEN`                | no       | API token the bot uses to authenticate to the admin UI (and the value the `taskiq_admin` compose service requires).                                  |
| `TASKIQ_ADMIN_BROKER_NAME`              | no       | Friendly broker name shown in the admin UI.                                                                                                          |

## Project Layout

```text
downloader-bot/
├── downloader_bot/         # Application package — drop new modules here
│   ├── bot.py              # Bot entry point: gateway client, cog loader, global error handler
│   ├── config.py           # pydantic-settings singleton loaded from .env
│   ├── embeds.py           # success/error/info/media_download embed helpers
│   ├── logging_setup.py    # init_logger() — one place to configure log format + level
│   ├── presence.py         # Status strings + the no-repeat picker used by bot.status_task
│   ├── tq.py               # Taskiq broker, scheduler, cancellation backend, worker startup hooks, typed dependency providers
│   ├── cogs/
│   │   ├── download.py     # /download — validates and enqueues, replies with a "queued" ack
│   │   ├── setup.py        # /setup — server-owner-only per-guild delivery config
│   │   ├── general.py      # /invite — DMs the configured INVITE_LINK
│   │   └── owner.py        # <PREFIX>sync (slash-command registration)
│   ├── tasks/
│   │   ├── __init__.py     # Re-exports download_channel_media for Taskiq worker discovery
│   │   └── download.py     # download_channel_media — the two-phase orchestration (upload → deliver)
│   ├── download/
│   │   ├── zip_stream.py   # build_zip_stream — async iterable of zip-encoded bytes
│   │   ├── deliver.py      # dm_user + post_to_channel (with DM fallback)
│   │   ├── idempotency.py  # Redis-backed phase guards keyed on Taskiq task_id
│   │   └── discord_rest.py # REST-only Discord client factory (login(), no gateway)
│   ├── worker/
│   │   └── healthcheck.py  # HeartbeatMiddleware + CLI probe used by the compose HEALTHCHECK
│   ├── db/
│   │   ├── schema.sql      # guild_settings table DDL + delivery-mode invariant CHECK
│   │   ├── pool.py         # build_pool() (opens pool + applies schema) + close_pool()
│   │   └── guild_settings.py  # GuildSettings dataclass + GuildSettingsRepo (async repo pattern)
│   └── storage/
│       ├── base.py         # StorageBackend ABC (upload_and_sign + delete_blob + async-CM)
│       ├── azure.py        # AzureBlobBackend — wraps Azure ContainerClient, handles Content-Disposition encoding
│       ├── __init__.py     # get_storage_backend() factory (lazy provider import)
│       └── exceptions.py   # Typed storage errors (config / upload / SAS)
├── scripts/
│   └── start.sh            # Production entrypoint (exec python -m downloader_bot.bot)
├── Dockerfile              # Multi-stage: builder → dev → prod
├── docker-compose.yml      # Dev stack: bot + worker + scheduler + rabbitmq + redis + postgres + azurite + taskiq_admin
├── docker-compose.prod.yml # Prod reference compose stack
├── pyproject.toml          # Project metadata + deps (azure backend via [azure] extra)
├── requirements-dev.txt    # Test/lint/pre-commit tooling (installs `.[azure]` editable)
└── .env.example
```

## Development

Tests, lint, and format are wired up via [pytest](https://docs.pytest.org/), [ruff](https://docs.astral.sh/ruff/), and [pre-commit](https://pre-commit.com/). Tests are unit-only with mocks for Discord, Azure, asyncpg, Redis, and Taskiq context — no compose stack needed to run them. Configuration lives in [pyproject.toml](pyproject.toml) (ruff + pytest + coverage) and [.pre-commit-config.yaml](.pre-commit-config.yaml).

### One-time setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
make install-dev                    # installs requirements-dev.txt + runs `pre-commit install`
```

`make install-dev` also wires up pre-commit so the ruff + whitespace hooks run on every `git commit`.

### Daily commands

| Make target                  | Equivalent direct invocation                                                        | What it does                                                   |
| ---------------------------- | ----------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| `make test`                  | `python -m pytest`                                                                  | Runs the test suite.                                           |
| `make test-cov`              | `python -m pytest --cov=downloader_bot --cov-report=term-missing --cov-report=html` | Runs tests with coverage; HTML report at `htmlcov/index.html`. |
| `make lint`                  | `python -m ruff check downloader_bot tests`                                         | Lints without modifying files.                                 |
| `make format`                | `python -m ruff format downloader_bot tests && python -m ruff check --fix ...`      | Auto-formats and applies safe lint fixes in place.             |
| `make format-check`          | `python -m ruff format --check downloader_bot tests`                                | Verifies formatting without writing — the CI-friendly check.   |
| `make check`                 | `lint` + `format-check` + `test` in sequence                                        | One-shot pre-push gate. Exits non-zero if anything fails.      |
| `make precommit`             | `pre-commit run --all-files`                                                        | Runs every pre-commit hook against the entire tree.            |
| `make dev` / `down` / `logs` | `docker compose up` / `down` / `logs -f bot worker`                                 | Compose-stack convenience targets.                             |
| `make clean`                 | `rm -rf .pytest_cache .ruff_cache .coverage htmlcov` + `__pycache__` sweep          | Wipes tooling caches.                                          |

> **Windows without `make`**: `make` isn't bundled with Git Bash. Either `choco install make` once, or copy-paste the right-hand "direct invocation" column. Every target is a one-liner so the fallback is mechanical.

### Recommended workflow for new code

1. **Branch off `main`** and start writing — keep your editor's ruff integration on if you have one (the [Ruff VS Code extension](https://marketplace.visualstudio.com/items?itemName=charliermarsh.ruff) reads `pyproject.toml` automatically).
2. **Write the test alongside the code.** Mirror the package layout under [tests/](tests/) — e.g. a change to [downloader_bot/tasks/download.py](downloader_bot/tasks/download.py) belongs in [tests/tasks/test_download.py](tests/tasks/test_download.py); zip-stream changes belong in [tests/download/test_zip_stream.py](tests/download/test_zip_stream.py). Use the existing fixtures in [tests/conftest.py](tests/conftest.py) (`mock_redis`, `make_db_pool`, `mock_discord_client`, `mock_storage_backend`, `make_settings_repo`, `task_context`, etc.) and the per-layer `conftest.py`s rather than re-mocking from scratch.
3. **Run a tight loop** while iterating:

   ```bash
   make test                                            # full suite
   python -m pytest tests/tasks/test_download.py -k "happy_path"   # narrower, while debugging one branch
   ```

4. **Format + lint before committing**:

   ```bash
   make format        # rewrites files in place — safe to run any time
   make check         # final gate: lint + format-check + test, exits non-zero on any failure
   ```

5. **Commit.** The pre-commit hooks (whitespace, end-of-file, ruff-check `--fix`, ruff-format) run automatically. If a hook *fixes* something, the commit aborts and the fixes are left unstaged — `git add` the changes and commit again. If a hook *fails* without auto-fixing, fix the issue and re-stage.
6. **Push.** No CI is wired up yet, so `make check` is your last line of defence. Run it before opening a PR.

A few things worth knowing about the test setup:

- `asyncio_mode = "auto"` in [pyproject.toml](pyproject.toml) means every `async def test_*` is treated as an asyncio test — no `@pytest.mark.asyncio` boilerplate.
- The cross-cutting [tests/conftest.py](tests/conftest.py) sets required env vars (`TOKEN`, `AZURE_CONN_STR`, `POSTGRES_DSN`, etc.) at module-body time, *before* `downloader_bot.*` is imported, because [downloader_bot/config.py](downloader_bot/config.py) constructs the `settings` singleton at import.
- For mocking `async for` over `channel.history(...)`, use the helpers in [tests/download/conftest.py](tests/download/conftest.py) — `AsyncMock` returns coroutines, which `async for` rejects.
- There is no coverage threshold yet (`--cov-fail-under` is intentionally unset). `make test-cov` is a baseline-tracking tool, not a gate.

## How It Works

`/download` is split between the bot and a Taskiq worker process so big-channel zips outlive Discord's 15-minute interaction-token window:

1. **Bot ack ([downloader_bot/cogs/download.py](downloader_bot/cogs/download.py)).** Validates the request (pre-checks `Read Message History` on the channel for the bot), then calls `download_channel_media.kiq(channel_id=..., user_id=..., guild_id=..., only_me=...)`. Taskiq publishes the task to RabbitMQ and returns an `AsyncTaskiqTask`; the cog uses its `task_id` to render the blurple "Download queued" embed.
2. **Worker pipeline ([downloader_bot/tasks/download.py](downloader_bot/tasks/download.py)).** A Taskiq worker pulls the task off the queue and runs two phases — each guarded by a Redis idempotency check so a retry skips already-completed work:
   1. **Upload.** Resolve the guild's settings (delivery mode, allowed-media filter, retention hours; missing rows get safe defaults). Walk channel history via [`build_zip_stream`](downloader_bot/download/zip_stream.py) — an async iterable of zip-encoded bytes that composes `channel.history()` → aiohttp chunked GETs → `stream-zip`'s async generator. Feed the iterable directly to [`StorageBackend.upload_and_sign`](downloader_bot/storage/base.py) (Azure today; S3/GCS in scope for future PRs), passing both a stable storage key (`channel-{channel_id}-{task_id}.zip`) and a friendly `download_filename` (e.g. `channel-general-2026-05-09.zip`) which the backend encodes into the SAS as a Content-Disposition override. A `try/finally` guarantees partial blobs are best-effort cleaned up on any failure path (including cancellation).
   2. **Deliver.** If the guild's mode is `channel` and a results channel is set, post the SAS URL there (mentioning the requester); otherwise DM the requester. Channel posts fall back to DM if the channel is missing, the bot lacks permission, or the channel isn't `Messageable`. The "delivered" marker is set **after** the send returns, so a crash mid-send re-delivers on retry (duplicate DM beats no DM).

Errors are handled close to their source: storage failures raise typed exceptions from [downloader_bot/storage/exceptions.py](downloader_bot/storage/exceptions.py); mid-flight attachment HTTP failures raise [`AttachmentStreamError`](downloader_bot/download/zip_stream.py); DM-disabled users raise [`DMUnavailable`](downloader_bot/download/deliver.py). Anything unexpected propagates out of the task; Taskiq's `SimpleRetryMiddleware` retries it (up to 3 times) under the same `task_id`, and the idempotency layer makes that safe.

Bot-side command errors are translated to user-facing embeds by a global handler in [downloader_bot/bot.py](downloader_bot/bot.py) using the helpers in [downloader_bot/embeds.py](downloader_bot/embeds.py), so cogs raise typed exceptions rather than formatting messages themselves. The `downloader_bot/cogs/setup.py` cog adds a cog-local handler for its custom `NotGuildOwner` check.

## Dependencies

- Python 3.12
- [discord.py](https://github.com/Rapptz/discord.py) 2.6.4
- [Taskiq](https://taskiq-python.github.io/) 0.12.3 with [`taskiq-aio-pika`](https://github.com/taskiq-python/taskiq-aio-pika) 0.6.0 (RabbitMQ broker), [`taskiq-redis`](https://github.com/taskiq-python/taskiq-redis) 1.2.2 (result backend + schedule source), [`taskiq-cancellation`](https://github.com/taskiq-python/taskiq-cancellation) 0.0.1, and [`taskiq-dependencies`](https://github.com/taskiq-python/taskiq-dependencies) 1.5.7
- [asyncpg](https://github.com/MagicStack/asyncpg) 0.30.0 (Postgres driver)
- [azure-storage-blob](https://pypi.org/project/azure-storage-blob/) 12.28.0
- [stream-zip](https://stream-zip.docs.trade.gov.uk/) ≥ 0.0.83
- [discordhealthcheck](https://github.com/psidex/DiscordHealthcheck) 0.1.1
- aiohttp, pydantic-settings, redis (async)
