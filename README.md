# Downloader Bot

A Discord bot that bundles every image (or other allowed media) in a channel into a single zip and hands the user a download link. The zip is uploaded to Azure Blob Storage and shared as a 1-hour SAS URL; if Azure is unavailable, the bot falls back to delivering the zip as a direct Discord attachment when it fits the server's upload limit.

## Commands

- **`/download [only_me]`** — Collects every attachment in the current channel that matches `ALLOWED_MEDIA_TYPES`, zips them, and replies with a download link. Set `only_me: true` to send the link as an ephemeral reply visible only to you.
- **`/sync`** — Owner-only. Re-registers slash commands globally or per-guild. Run this after deploying new commands.

Both commands are hybrid — they work with the configured `PREFIX` (e.g. `??download`) as well as the slash-command UI.

## Quick Start (Development)

The dev environment uses Docker Compose to run the bot alongside [Azurite](https://github.com/Azure/Azurite), Microsoft's local Azure Blob Storage emulator, so no real Azure account is needed.

```bash
cp .env.example .env
# Fill in TOKEN at minimum. For Azurite, set:
#   ENVIRONMENT=dev
#   ST_INT_URL=http://azurite:10000/devstoreaccount1
#   ST_EXT_URL=http://localhost:10000/devstoreaccount1
#   ST_CONN_STR=DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://azurite:10000/devstoreaccount1;

docker compose up
```

The dev image runs the bot under [watchfiles](https://watchfiles.helpmanual.io/), so edits to [bot.py](bot.py), [cogs/](cogs/), or [storage/](storage/) trigger an automatic restart. The repo is mounted into `/bot/` inside the container.

When `ENVIRONMENT=dev`, generated SAS URLs are rewritten from `ST_INT_URL` (the in-network Azurite hostname) to `ST_EXT_URL` (the host-reachable one) so links opened in your browser actually resolve — see [cogs/download.py:256-259](cogs/download.py#L256-L259).

## Production

Build the image and run it against a real Azure Storage account.

```bash
# x86_64
docker build -t downloader-bot:<VERSION> .

# Raspberry Pi / ARM64
docker build --platform linux/arm64/v8 -t downloader-bot:<VERSION>-arm64-v8 .

docker run -d --name downloader-bot --env-file .env.prod downloader-bot:<VERSION>
```

The production image:

- Runs as a non-root `discordbot` user
- Uses [Tini](https://github.com/krallin/tini) as PID 1 for proper signal handling and zombie reaping
- Exposes a Docker `HEALTHCHECK` via [discordhealthcheck](https://github.com/psidex/DiscordHealthcheck) that verifies the gateway connection every 60s

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
| `ST_INT_URL`          | dev only | Internal Azure Storage URL — the hostname the bot uses to reach the storage backend (e.g. `http://azurite:10000/devstoreaccount1`).    |
| `ST_EXT_URL`          | dev only | External Azure Storage URL — the hostname end users will use to download from generated SAS URLs.                                      |
| `LOGGING_LEVEL`       | no       | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Defaults to `INFO`.                                                                               |
| `INVITE_LINK`         | no       | Stored on the bot instance for reference; not currently surfaced to users.                                                             |

## Project Layout

```cmd
downloader-bot/
├── bot.py              # Entry point: bot class, cog loader, global error handler
├── cogs/
│   ├── download.py     # /download command — the core feature
│   └── owner.py        # /sync command for slash-command registration
├── storage/
│   ├── container.py    # Async repository wrapping Azure ContainerClient
│   └── exceptions.py   # Typed storage errors (config / upload / SAS)
├── scripts/
│   └── start.sh        # Production entrypoint
├── Dockerfile          # Multi-stage: builder → dev → prod
├── docker-compose.yml  # Dev stack: bot + Azurite + container init
├── requirements.txt
└── .env.example
```

## How It Works

The `/download` command runs in four phases ([cogs/download.py](cogs/download.py)):

1. **Collect.** Walk channel history, filter attachments by `ALLOWED_MEDIA_TYPES`, stream each one into an in-memory `ZipFile` keyed by `{message_id}_{filename}`. Per-attachment failures are logged and skipped — one bad file doesn't abort the run.
2. **Validate.** If no media was found, reply with an error and stop.
3. **Upload.** Push the zip to Azure via [`ContainerRepository`](storage/container.py) and generate a 1-hour SAS URL.
4. **Reply.** Send a success embed with the link and a count of bundled files.

If step 3 fails with a `BlobUploadError` or `SasGenerationError` and the zip is small enough to fit the guild's upload limit (8 MB / 50 MB / 100 MB depending on boost tier), the bot falls back to attaching the zip directly to its reply. `ContainerConfigError` (missing storage credentials) is non-recoverable and surfaces as a hard error.

All command errors are translated to user-facing embeds by a global handler in [bot.py:192-366](bot.py#L192-L366), so cogs raise typed exceptions rather than formatting messages themselves.

## Dependencies

- Python 3.12
- [discord.py](https://github.com/Rapptz/discord.py) 2.6.4
- [azure-storage-blob](https://pypi.org/project/azure-storage-blob/) 12.28.0
- [discordhealthcheck](https://github.com/psidex/DiscordHealthcheck) 0.1.1
- aiohttp, python-dotenv
