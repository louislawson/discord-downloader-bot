# Downloader Bot

A Discord bot that bundles every image (or other allowed media) in a channel into a single zip and hands the user a download link. The zip is uploaded to Azure Blob Storage and shared as a 1-hour SAS URL; if Azure is unavailable, the bot falls back to delivering the zip as a direct Discord attachment when it fits the server's upload limit.

The bot itself only enqueues jobs — a separate ARQ worker (same image, different `CMD`) runs the channel-history walk, zipping, upload, and delivery. This lets downloads outlive Discord's 15-minute interaction-token window and keeps long-running downloads in one channel from blocking another.

## Commands

- **`/download [only_me]`** — Queues a job that collects every attachment in the current channel matching `ALLOWED_MEDIA_TYPES`, zips them, and delivers a download link. Set `only_me: true` to force private DM delivery (overrides the server's configured mode).
- **`/setup mode <dm|channel|both>`** — Server-owner only. Sets how completed downloads are delivered. `dm` sends to the requester only; `channel` posts in a configured channel mentioning them; `both` DMs first and falls back to the channel if the DM is blocked.
- **`/setup channel <#channel>`** — Server-owner only. Sets the channel used by `channel` mode (and as the fallback for `both`).
- **`/setup clear`** — Server-owner only. Unsets the configured results channel.
- **`/setup show`** — Server-owner only. Displays current delivery settings.
- **`/sync`** — Bot-owner only. Re-registers slash commands globally or per-guild. Run this after deploying new commands.

All commands are hybrid — they work with the configured `PREFIX` (e.g. `??download`) as well as the slash-command UI.

## Quick Start (Development)

The dev environment uses Docker Compose to run the bot, an ARQ worker, Redis (job queue), Postgres (per-guild settings), and [Azurite](https://github.com/Azure/Azurite) (Microsoft's local Azure Blob Storage emulator), so no real Azure account is needed.

```bash
cp .env.example .env
# Fill in TOKEN at minimum. For Azurite, set:
#   ENVIRONMENT=dev
#   AZURE_INT_URL=http://azurite:10000/devstoreaccount1
#   AZURE_EXT_URL=http://localhost:10000/devstoreaccount1
#   AZURE_CONN_STR=DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://azurite:10000/devstoreaccount1;
# REDIS_URL and POSTGRES_DSN already point at the compose services.

docker compose up --build -d
```

Both the bot and worker run under [watchfiles](https://watchfiles.helpmanual.io/), so any `.py` change under [downloader_bot/](downloader_bot/) triggers an automatic restart. The repo is mounted into `/bot/` inside both containers.

When `ENVIRONMENT=dev`, generated SAS URLs are rewritten from `AZURE_INT_URL` (the in-network Azurite hostname) to `AZURE_EXT_URL` (the host-reachable one) so links opened in your browser actually resolve — see [downloader_bot/worker/jobs.py](downloader_bot/worker/jobs.py).

## Production

Build the image and run two containers from it (bot + worker), pointing both at the same Redis, Postgres, and Azure Storage.

```bash
# x86_64
docker build -t downloader-bot:<VERSION> .

# Raspberry Pi / ARM64
docker build --platform linux/arm64/v8 -t downloader-bot:<VERSION>-arm64-v8 .

# Bot (gateway-connected)
docker run -d --name downloader-bot --env-file .env.prod downloader-bot:<VERSION>

# Worker (REST-only, runs the actual downloads)
docker run -d --name downloader-bot-worker --env-file .env.prod \
  downloader-bot:<VERSION> arq downloader_bot.worker.main.WorkerSettings
```

You'll also need Redis and Postgres reachable from both containers (managed services or self-hosted; the dev compose file shows the minimum config). The bot bootstraps the Postgres schema idempotently on startup — no migration step required.

The production image:

- Runs as a non-root `discordbot` user
- Uses [Tini](https://github.com/krallin/tini) as PID 1 for proper signal handling and zombie reaping
- Exposes a Docker `HEALTHCHECK` via [discordhealthcheck](https://github.com/psidex/DiscordHealthcheck) that verifies the gateway connection every 60s (bot only — the worker has no gateway to check)

For production, set `ENVIRONMENT=prod` (disables the SAS URL rewrite) and point `AZURE_CONN_STR` at your real storage account.

## Configuration

All configuration is loaded from `.env` via `python-dotenv`. Copy `.env.example` and fill in the values.

| Variable              | Required | Description                                                                                                                            |
| --------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `TOKEN`               | yes      | Discord bot token.                                                                                                                     |
| `PREFIX`              | yes      | Prefix for text commands (e.g. `??`). Slash commands always work regardless.                                                           |
| `ENVIRONMENT`         | yes      | `prod` or `dev`. Toggles the SAS URL hostname rewrite.                                                                                 |
| `ALLOWED_MEDIA_TYPES` | yes      | JSON array of MIME types to collect (see `.env.example`). Attachments outside this list are silently skipped.                          |
| `AZURE_CONN_STR`         | yes      | Azure Blob Storage connection string. SAS URL generation requires this to contain an account key.                                      |
| `AZURE_CONTAINER`        | yes      | Blob container name. The dev compose stack auto-creates one called `media`.                                                            |
| `POSTGRES_DSN`        | yes      | asyncpg DSN for the Postgres instance holding per-guild settings. Defaults to the compose-stack value.                                 |
| `REDIS_URL`           | yes      | URL of the Redis broker used as the ARQ job queue. Defaults to the compose-stack value.                                                |
| `AZURE_INT_URL`          | dev only | Internal Azure Storage URL — the hostname the bot uses to reach the storage backend (e.g. `http://azurite:10000/devstoreaccount1`).    |
| `AZURE_EXT_URL`          | dev only | External Azure Storage URL — the hostname end users will use to download from generated SAS URLs.                                      |
| `LOGGING_LEVEL`       | no       | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Defaults to `INFO`.                                                                               |
| `INVITE_LINK`         | no       | Stored on the bot instance for reference; not currently surfaced to users.                                                             |

## Project Layout

```text
downloader-bot/
├── downloader_bot/         # Application package — drop new modules here
│   ├── bot.py              # Bot entry point: gateway client, cog loader, global error handler
│   ├── config.py           # pydantic-settings singleton loaded from .env
│   ├── queue_client.py     # ARQ pool factory used by the bot to enqueue jobs
│   ├── cogs/
│   │   ├── download.py     # /download — validates and enqueues, replies with a "queued" ack
│   │   ├── setup.py        # /setup — server-owner-only per-guild delivery config
│   │   └── owner.py        # /sync command for slash-command registration
│   ├── worker/
│   │   ├── main.py         # ARQ WorkerSettings + on_startup/on_shutdown hooks
│   │   ├── jobs.py         # download_channel_media job (the four-phase pipeline)
│   │   ├── delivery.py     # DM/channel routing + Redis-backed idempotency
│   │   └── discord_rest.py # REST-only Discord client factory used by the worker
│   ├── db/
│   │   ├── schema.sql      # guild_settings table DDL (bootstrapped on bot startup)
│   │   ├── pool.py         # asyncpg pool factory + init_schema runner
│   │   └── guild_settings.py  # Read/write API for delivery mode + results channel
│   └── storage/
│       ├── base.py         # StorageBackend ABC (upload_and_sign + async-CM)
│       ├── azure.py        # AzureBlobBackend — wraps Azure ContainerClient
│       ├── __init__.py     # get_storage_backend() factory (lazy provider import)
│       └── exceptions.py   # Typed storage errors (config / upload / SAS)
├── scripts/
│   └── start.sh            # Production entrypoint
├── Dockerfile              # Multi-stage: builder → dev → prod
├── docker-compose.yml      # Dev stack: bot + worker + redis + postgres + azurite
├── pyproject.toml          # Project metadata + deps (azure backend via [azure] extra)
├── requirements-dev.txt    # Test/lint/pre-commit tooling (installs `.[azure]` editable)
└── .env.example
```

## Development

Tests, lint, and format are wired up via [pytest](https://docs.pytest.org/), [ruff](https://docs.astral.sh/ruff/), and [pre-commit](https://pre-commit.com/). Tests are unit-only with mocks for Discord, Azure, asyncpg, and ARQ-Redis — no compose stack needed to run them. Configuration lives in [pyproject.toml](pyproject.toml) (ruff + pytest + coverage) and [.pre-commit-config.yaml](.pre-commit-config.yaml).

### One-time setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
make install-dev                    # installs requirements-dev.txt + runs `pre-commit install`
```

`make install-dev` also wires up pre-commit so the ruff + whitespace hooks run on every `git commit`.

### Daily commands

| Make target           | Equivalent direct invocation                                                          | What it does                                                            |
| --------------------- | ------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `make test`           | `python -m pytest`                                                                    | Runs the test suite.                                                    |
| `make test-cov`       | `python -m pytest --cov=downloader_bot --cov-report=term-missing --cov-report=html`   | Runs tests with coverage; HTML report at `htmlcov/index.html`.          |
| `make lint`           | `python -m ruff check downloader_bot tests`                                           | Lints without modifying files.                                          |
| `make format`         | `python -m ruff format downloader_bot tests && python -m ruff check --fix ...`        | Auto-formats and applies safe lint fixes in place.                      |
| `make format-check`   | `python -m ruff format --check downloader_bot tests`                                  | Verifies formatting without writing — the CI-friendly check.            |
| `make check`          | `lint` + `format-check` + `test` in sequence                                          | One-shot pre-push gate. Exits non-zero if anything fails.               |
| `make precommit`      | `pre-commit run --all-files`                                                          | Runs every pre-commit hook against the entire tree.                     |
| `make clean`          | `rm -rf .pytest_cache .ruff_cache .coverage htmlcov` + `__pycache__` sweep            | Wipes tooling caches.                                                   |

> **Windows without `make`**: `make` isn't bundled with Git Bash. Either `choco install make` once, or copy-paste the right-hand "direct invocation" column. Every target is a one-liner so the fallback is mechanical.

### Recommended workflow for new code

1. **Branch off `main`** and start writing — keep your editor's ruff integration on if you have one (the [Ruff VS Code extension](https://marketplace.visualstudio.com/items?itemName=charliermarsh.ruff) reads `pyproject.toml` automatically).
2. **Write the test alongside the code.** Mirror the package layout under [tests/](tests/) — e.g. a change to [downloader_bot/worker/jobs.py](downloader_bot/worker/jobs.py) belongs in [tests/worker/test_jobs.py](tests/worker/test_jobs.py). Use the existing fixtures in [tests/conftest.py](tests/conftest.py) and the per-layer `conftest.py`s rather than re-mocking from scratch.
3. **Run a tight loop** while iterating:

   ```bash
   make test                                 # full suite, ~1.5s
   python -m pytest tests/worker/test_jobs.py -k "happy_path"   # narrower, while debugging one branch
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
- For mocking `async for` over `channel.history(...)`, use the `_AsyncIter` helper in [tests/worker/conftest.py](tests/worker/conftest.py) — `AsyncMock` returns coroutines, which `async for` rejects.
- There is no coverage threshold yet (`--cov-fail-under` is intentionally unset). `make test-cov` is a baseline-tracking tool, not a gate.

## How It Works

`/download` is split between the bot and a worker process so big-channel zips outlive Discord's 15-minute interaction-token window:

1. **Bot ack ([downloader_bot/cogs/download.py](downloader_bot/cogs/download.py)).** Validates the request, builds a JSON payload (channel id, requester, `only_me`, allowed MIME types), and calls `arq_pool.enqueue_job("download_channel_media", payload, _job_id=...)`. Replies immediately with a blurple "Download queued" embed.
2. **Worker pipeline ([downloader_bot/worker/jobs.py](downloader_bot/worker/jobs.py)).** ARQ picks up the job and runs four phases:
   1. **Collect.** Walk channel history, filter attachments by `ALLOWED_MEDIA_TYPES`, stream each one into an in-memory `ZipFile` keyed by `{message_id}_{filename}`. Per-attachment failures are logged and skipped — one bad file doesn't abort the run.
   2. **Validate.** If no media was found, deliver a red error embed and stop.
   3. **Upload.** Push the zip via the configured [`StorageBackend`](downloader_bot/storage/base.py) (Azure today; S3/GCS in scope for future PRs) and generate a 1-hour pre-signed URL via `upload_and_sign()`.
   4. **Deliver.** Send a green success embed with the link and a count of bundled files via [`downloader_bot/worker/delivery.py`](downloader_bot/worker/delivery.py), which routes between DM and the configured guild channel based on per-guild `/setup` settings (Postgres-backed). A Redis SET-NX claim keyed `delivered:{job_id}` makes delivery idempotent across ARQ retries.

If the upload fails with `UploadError`/`SignedUrlError` and the zip fits the guild's upload limit (8 MB / 50 MB / 100 MB depending on boost tier), the worker falls back to delivering the zip as a direct Discord attachment with an orange "Cloud storage unavailable" embed. `StorageConfigError` (missing storage credentials) is non-recoverable and surfaces as a hard error. Any other unhandled exception in the worker (transient Discord 5xx, network blips, unexpected SDK errors) gets a generic "Download failed" embed delivered to the requester before the exception re-raises, so a failed job never leaves the user staring at a "queued" message forever.

Bot-side command errors are translated to user-facing embeds by a global handler in [downloader_bot/bot.py](downloader_bot/bot.py), so cogs raise typed exceptions rather than formatting messages themselves. The `downloader_bot/cogs/setup.py` cog adds a cog-local handler for its custom `NotGuildOwner` check.

## Dependencies

- Python 3.12
- [discord.py](https://github.com/Rapptz/discord.py) 2.6.4
- [ARQ](https://arq-docs.helpmanual.io/) 0.26.3 (Redis-backed job queue)
- [asyncpg](https://github.com/MagicStack/asyncpg) 0.30.0 (Postgres driver)
- [azure-storage-blob](https://pypi.org/project/azure-storage-blob/) 12.28.0
- [discordhealthcheck](https://github.com/psidex/DiscordHealthcheck) 0.1.1
- aiohttp, pydantic-settings
