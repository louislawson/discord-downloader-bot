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
#   ST_INT_URL=http://azurite:10000/devstoreaccount1
#   ST_EXT_URL=http://localhost:10000/devstoreaccount1
#   ST_CONN_STR=DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://azurite:10000/devstoreaccount1;
# REDIS_URL and POSTGRES_DSN already point at the compose services.

docker compose up --build -d
```

Both the bot and worker run under [watchfiles](https://watchfiles.helpmanual.io/), so any `.py` change under [downloader_bot/](downloader_bot/) triggers an automatic restart. The repo is mounted into `/bot/` inside both containers.

When `ENVIRONMENT=dev`, generated SAS URLs are rewritten from `ST_INT_URL` (the in-network Azurite hostname) to `ST_EXT_URL` (the host-reachable one) so links opened in your browser actually resolve — see [downloader_bot/worker/jobs.py](downloader_bot/worker/jobs.py).

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

For production, set `ENVIRONMENT=prod` (disables the SAS URL rewrite) and point `ST_CONN_STR` at your real storage account.

## Configuration

All configuration is loaded from `.env` via `python-dotenv`. Copy `.env.example` and fill in the values.

| Variable              | Required | Description                                                                                                                            |
| --------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `TOKEN`               | yes      | Discord bot token.                                                                                                                     |
| `PREFIX`              | yes      | Prefix for text commands (e.g. `??`). Slash commands always work regardless.                                                           |
| `ENVIRONMENT`         | yes      | `prod` or `dev`. Toggles the SAS URL hostname rewrite.                                                                                 |
| `ALLOWED_MEDIA_TYPES` | yes      | JSON array of MIME types to collect (see `.env.example`). Attachments outside this list are silently skipped.                          |
| `ST_CONN_STR`         | yes      | Azure Blob Storage connection string. SAS URL generation requires this to contain an account key.                                      |
| `ST_CONTAINER`        | yes      | Blob container name. The dev compose stack auto-creates one called `media`.                                                            |
| `POSTGRES_DSN`        | yes      | asyncpg DSN for the Postgres instance holding per-guild settings. Defaults to the compose-stack value.                                 |
| `REDIS_URL`           | yes      | URL of the Redis broker used as the ARQ job queue. Defaults to the compose-stack value.                                                |
| `ST_INT_URL`          | dev only | Internal Azure Storage URL — the hostname the bot uses to reach the storage backend (e.g. `http://azurite:10000/devstoreaccount1`).    |
| `ST_EXT_URL`          | dev only | External Azure Storage URL — the hostname end users will use to download from generated SAS URLs.                                      |
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
│       ├── container.py    # Async repository wrapping Azure ContainerClient
│       └── exceptions.py   # Typed storage errors (config / upload / SAS)
├── scripts/
│   └── start.sh            # Production entrypoint
├── Dockerfile              # Multi-stage: builder → dev → prod
├── docker-compose.yml      # Dev stack: bot + worker + redis + postgres + azurite
├── requirements.txt
└── .env.example
```

## How It Works

`/download` is split between the bot and a worker process so big-channel zips outlive Discord's 15-minute interaction-token window:

1. **Bot ack ([downloader_bot/cogs/download.py](downloader_bot/cogs/download.py)).** Validates the request, builds a JSON payload (channel id, requester, `only_me`, allowed MIME types), and calls `arq_pool.enqueue_job("download_channel_media", payload, _job_id=...)`. Replies immediately with a blurple "Download queued" embed.
2. **Worker pipeline ([downloader_bot/worker/jobs.py](downloader_bot/worker/jobs.py)).** ARQ picks up the job and runs four phases:
   1. **Collect.** Walk channel history, filter attachments by `ALLOWED_MEDIA_TYPES`, stream each one into an in-memory `ZipFile` keyed by `{message_id}_{filename}`. Per-attachment failures are logged and skipped — one bad file doesn't abort the run.
   2. **Validate.** If no media was found, deliver a red error embed and stop.
   3. **Upload.** Push the zip to Azure via [`ContainerRepository`](downloader_bot/storage/container.py) and generate a 1-hour SAS URL.
   4. **Deliver.** Send a green success embed with the link and a count of bundled files via [`downloader_bot/worker/delivery.py`](downloader_bot/worker/delivery.py), which routes between DM and the configured guild channel based on per-guild `/setup` settings (Postgres-backed). A Redis SET-NX claim keyed `delivered:{job_id}` makes delivery idempotent across ARQ retries.

If the upload fails with `BlobUploadError`/`SasGenerationError` and the zip fits the guild's upload limit (8 MB / 50 MB / 100 MB depending on boost tier), the worker falls back to delivering the zip as a direct Discord attachment with an orange "Azure unavailable" embed. `ContainerConfigError` (missing storage credentials) is non-recoverable and surfaces as a hard error. Any other unhandled exception in the worker (transient Discord 5xx, network blips, unexpected SDK errors) gets a generic "Download failed" embed delivered to the requester before the exception re-raises, so a failed job never leaves the user staring at a "queued" message forever.

Bot-side command errors are translated to user-facing embeds by a global handler in [downloader_bot/bot.py](downloader_bot/bot.py), so cogs raise typed exceptions rather than formatting messages themselves. The `downloader_bot/cogs/setup.py` cog adds a cog-local handler for its custom `NotGuildOwner` check.

## Dependencies

- Python 3.12
- [discord.py](https://github.com/Rapptz/discord.py) 2.6.4
- [ARQ](https://arq-docs.helpmanual.io/) 0.26.3 (Redis-backed job queue)
- [asyncpg](https://github.com/MagicStack/asyncpg) 0.30.0 (Postgres driver)
- [azure-storage-blob](https://pypi.org/project/azure-storage-blob/) 12.28.0
- [discordhealthcheck](https://github.com/psidex/DiscordHealthcheck) 0.1.1
- aiohttp, pydantic-settings
