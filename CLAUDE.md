# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Discord bot that bundles every allowed-MIME attachment in a channel into one in-memory zip and delivers it via a 1-hour Azure Blob Storage SAS URL. If Azure is unavailable, it falls back to sending the zip as a direct Discord attachment when the file fits the guild's upload tier (8/50/100 MB). The bot only enqueues jobs — an ARQ worker (separate compose service, same image) does the channel-history walk, zipping, upload, and delivery, so the work outlives the 15-minute Discord interaction-token window and parallel `/download`s in different channels don't block each other. Python 3.12, discord.py 2.6.4, azure-storage-blob 12.28.0, ARQ 0.26.3, asyncpg 0.30.0, pydantic-settings 2.14.0.

## Common commands

Dev — auto-reloads on any `.py` change under `downloader_bot/` via watchfiles:

```bash
cp .env.example .env   # fill in TOKEN; for Azurite see README quickstart
docker compose up      # bot + worker + redis + postgres + azurite (auto-creates `media`)
```

Production:

```bash
docker build -t downloader-bot:<VERSION> .
docker run -d --name downloader-bot --env-file .env.prod downloader-bot:<VERSION>
```

ARM64 (e.g. Raspberry Pi): `docker build --platform linux/arm64/v8 -t downloader-bot:<VERSION>-arm64-v8 .`

Tests / lint / format are wired through a `Makefile` (pytest 8.4.2, ruff 0.14.4, pre-commit 4.4.0; configured in `pyproject.toml` and `.pre-commit-config.yaml`):

```bash
make install-dev    # installs requirements-dev.txt + runs `pre-commit install`
make test           # python -m pytest
make test-cov       # pytest with coverage; HTML report at htmlcov/index.html
make lint           # ruff check (no edits)
make format         # ruff format + ruff check --fix in place
make check          # lint + format-check + test — the pre-push gate
```

Tests are unit-only with mocks for Discord, Azure, asyncpg, and ARQ-Redis — **no compose stack is required to run them**. There is no CI yet, so `make check` is the only gate before merging.

## Architecture

### Two processes, one image

The same Docker image runs as two compose services with different `CMD`s:

- **bot** — connects to the Discord gateway, exposes slash/prefix commands, opens an asyncpg pool to Postgres and an ARQ pool to Redis. Commands enqueue jobs and ack the user; they don't do work.
- **worker** — `arq downloader_bot.worker.main.WorkerSettings`. REST-only Discord client (no gateway), shared asyncpg pool, runs the four-phase pipeline below. Multiple workers can run in parallel; per-channel ordering isn't guaranteed (the bot uses unique `_job_id`s, not channel-keyed locks).

The split exists because zipping a busy channel routinely takes longer than Discord's 15-minute interaction-token window. Decoupling also lets independent channels run in parallel.

### Entry point

`downloader_bot/bot.py` defines `DiscordBot(commands.Bot)`. `setup_hook` opens the asyncpg pool, runs `init_schema(pool)` (idempotent `CREATE TABLE IF NOT EXISTS` from `downloader_bot/db/schema.sql`), then calls `load_cogs()`, which scans `downloader_bot/cogs/` and `load_extension()`s every `.py` file dynamically (as `downloader_bot.cogs.<name>`). The ARQ pool is opened after cogs load. Adding a cog = drop a file in `downloader_bot/cogs/` with an `async def setup(bot)` — no registration list to update.

### Cogs

- `downloader_bot/cogs/download.py` — `@commands.hybrid_command` `/download`. Validates, builds a JSON-serialisable payload, calls `arq_pool.enqueue_job("download_channel_media", payload, _job_id=job_id)`, replies with a blurple "Download queued" ack. Two service-unavailable paths: `arq_pool is None` (Redis unreachable at startup) and an `enqueue_job` that raises mid-flight (Redis went away after startup) — both surface the same red embed.
- `downloader_bot/cogs/setup.py` — `@commands.hybrid_group` `/setup` with `mode | channel | clear | show` subcommands, server-owner-gated via a custom `_is_guild_owner` predicate that raises `NotGuildOwner(commands.CheckFailure)`. Cog-local `cog_command_error` handles `NotGuildOwner` and `NoPrivateMessage` so they don't leak to the global handler.
- `downloader_bot/cogs/owner.py` — owner-only `<PREFIX>sync global|guild` for slash-command registration. Run this after adding or changing a hybrid command before slash UI updates.
- `downloader_bot/cogs/general.py` — `@commands.hybrid_command` `/invite`. Sends the bot's invite link via DM, falls back to an ephemeral channel reply if DMs are blocked.

### Worker (`downloader_bot/worker/`)

- `downloader_bot/worker/main.py` — defines `WorkerSettings` (the ARQ entrypoint discovered by import path) and `on_startup`/`on_shutdown` hooks. `on_startup` opens a REST-only Discord client and an asyncpg pool, stashing both on `ctx` (`ctx['discord_client']`, `ctx['db_pool']`).
- `downloader_bot/worker/jobs.py` — `download_channel_media(ctx, payload)` is the public ARQ function. It's a thin wrapper around `_run_download_channel_media` that catches any unhandled exception, calls `deliver()` with a generic "Download failed" embed, then re-raises so ARQ records the failure. **ARQ's `max_tries` only governs `Retry`/`RetryJob`-driven retries** — arbitrary `Exception`s go straight to a permanent `! ... failed` ([arq/worker.py:594-625](https://github.com/python-arq/arq/blob/v0.26.3/arq/worker.py)), so by the time the wrapper sees an exception, the job is over. `Retry`/`RetryJob` are re-raised untouched so explicit retry signaling still works if a future code path uses it. Anticipated errors (Forbidden, StorageConfigError, UploadError + fallback) are handled inside the body and produce their own targeted embeds. The four-phase pipeline lives here:
  1. **Collect** — `channel.history(limit=None)`, filter by `payload['allowed_media_types']`, stream each attachment into one in-memory `ZipFile` keyed `{message_id}_{filename}`. Per-attachment HTTP errors are logged and skipped.
  2. **Validate** — zero media → red error embed via `deliver`, return.
  3. **Upload** — `async with get_storage_backend()` → `upload_and_sign()`. The Azure backend handles the dev-mode SAS host rewrite (`AZURE_INT_URL` → `AZURE_EXT_URL`) internally.
  4. **Deliver** — green success embed via `deliver`.
- `downloader_bot/worker/delivery.py` — `deliver(...)` decides DM vs channel post per the guild's `delivery_mode` (Postgres-backed, see `downloader_bot/db/`). Idempotent via a Redis `delivered:{job_id}` SET-NX claim with 24h TTL, so ARQ retries don't double-send. Decision tree: `only_me=True` → DM only (fail-closed on Forbidden). `mode=dm` → DM only, fail-closed. `mode=channel` with no channel set → fall back to DM, fail-closed. `mode=both` → DM first, channel on Forbidden; if no channel and DM blocked, drop with a warning.
- `downloader_bot/worker/discord_rest.py` — opens a REST-only `discord.Client` (no gateway, no intents) for the worker, since the worker only needs to fetch channels/users and send messages.

### Fallback ladder (`downloader_bot/worker/jobs.py`)

- `UploadError` / `SignedUrlError` → if `zip_size <= guild_upload_limit`, deliver the zip as a Discord attachment with an orange "Cloud storage unavailable" embed; otherwise deliver a hard-error embed.
- `StorageConfigError` is **non-recoverable** (deployment misconfig). Never falls back; always surfaces as a hard error.
- Guild upload limit comes from `_guild_upload_limit()` (worker-local copy of the boost-tier table) — Tier 0/1: 8 MB, Tier 2: 50 MB, Tier 3: 100 MB, DM/unknown: 8 MB.
- Guild upload limit needs a real `Guild` (REST `fetch_guild`) — `client.fetch_channel(...)`'s `.guild` is a placeholder `Object` in REST-only mode and lacks `premium_tier`.

### Database (`downloader_bot/db/`)

Single-table Postgres schema bootstrapped via `CREATE TABLE IF NOT EXISTS` on bot startup — alembic deferred until there's more than one table to manage.

- `downloader_bot/db/schema.sql` — `guild_settings(guild_id BIGINT PK, delivery_mode TEXT CHECK IN ('dm','channel','both') DEFAULT 'dm', results_channel_id BIGINT, updated_at TIMESTAMPTZ DEFAULT now())`.
- `downloader_bot/db/pool.py` — `open_pool()` and `init_schema(pool)`. The bot calls both in `setup_hook`; the worker only calls `open_pool()` (the bot owns schema bootstrap).
- `downloader_bot/db/guild_settings.py` — read/write API (`get`, `set_mode`, `set_channel`, `clear_channel`). `get(pool, guild_id=None)` and `get` for an unconfigured guild both return `("dm", None)` — the safe default. UPSERTs use `ON CONFLICT (guild_id) DO UPDATE`.

### Error handling pattern

Cogs raise **typed exceptions**; the global `on_command_error` handler in `downloader_bot/bot.py` translates them into user-facing embeds. Don't write per-cog error embeds for storage failures — raise from `downloader_bot/storage/exceptions.py` (`StorageConfigError`, `UploadError`, `SignedUrlError`) and let the central handler format the message. discord.py's built-in errors (`CommandOnCooldown`, `MissingPermissions`, `BadArgument`, etc.) are also handled centrally. Cog-local `cog_command_error` is fine for cog-specific custom checks (see `downloader_bot/cogs/setup.py`'s `NotGuildOwner` handling).

### Storage layer (`downloader_bot/storage/`)

- `downloader_bot/storage/base.py` — `StorageBackend` ABC. Implementations expose `upload_and_sign(name, data, *, ttl, overwrite) -> str` and async-CM dunders. The worker imports the ABC, never a concrete provider module.
- `downloader_bot/storage/__init__.py` — `get_storage_backend()` is the dispatcher. It reads `settings.STORAGE_BACKEND` and lazily imports the matching provider module so deployments without that provider's SDK installed never fail to import the package.
- `downloader_bot/storage/azure.py` — `AzureBlobBackend` is the only concrete implementation today. Wraps `azure.storage.blob.aio.ContainerClient`; constructor takes an optional `client=` for dependency-injected mocks. Reads `settings.AZURE_CONN_STR` / `AZURE_CONTAINER`. SAS generation requires the connection string to carry an `AccountKey` — SAS-token-only or MSI credentials raise `SignedUrlError` at first call, not at startup. All `AzureError`s are caught and re-raised as `UploadError` / `SignedUrlError` so callers don't import the Azure SDK to handle failures.

### Config (`downloader_bot/config.py`)

pydantic-settings `Settings` model loaded from `.env`. Import the singleton: `from downloader_bot.config import settings`. Never re-read env vars with `os.getenv` — go through `settings`. `ALLOWED_MEDIA_TYPES` is parsed as JSON automatically by pydantic. New post-phase-2/3 keys: `REDIS_URL` (ARQ broker), `POSTGRES_DSN` (asyncpg). The README env-var table is the canonical reference.

## Conventions worth knowing

- **Adding a top-level Python file or directory**: drop it under `downloader_bot/`. The prod `Dockerfile` ships the entire package via `COPY /downloader_bot /bot/downloader_bot`; the dev image's `CMD` and the worker compose `command:` both watch the same directory. No Docker / compose changes are required for new files.
- **Imports are absolute and package-qualified.** Use `from downloader_bot.config import settings`, `from downloader_bot.db.pool import open_pool`, etc. The bot is run as `python -m downloader_bot.bot` and the worker as `arq downloader_bot.worker.main.WorkerSettings`, both with `WORKDIR=/bot` so the package resolves on `sys.path`.
- **Every directory under `downloader_bot/` is a regular package** with an `__init__.py` (usually empty). Don't add namespace-package tricks — keep these as plain packages so static analysers and IDE tooling resolve them cleanly.
- **Job payloads must be JSON-serialisable.** ARQ pickles by default but we keep payloads JSON-friendly (ISO timestamps as strings, lists not sets) so they're inspectable in Redis and survive worker version skew.
- **Schema changes** today mean editing `downloader_bot/db/schema.sql` and shipping; it re-runs idempotently on bot startup. If a migration ever needs to drop a column or backfill, that's the point at which alembic earns its keep — don't add it preemptively.
- **All I/O is async.** Don't introduce synchronous storage-SDK or `requests` calls; use the SDK's async client (`azure.storage.blob.aio` for the Azure backend) and `aiohttp` everywhere else.
- **Keep the zip in-memory.** The `BytesIO` + streaming-into-`ZipFile` pattern in `downloader_bot/worker/jobs.py` is deliberate — there is no scratch directory, and adding one would regress an earlier optimisation.
- **Hybrid commands** (`@commands.hybrid_command`/`@commands.hybrid_group`) need an owner to run `<PREFIX>sync global` (or `guild` for instant local testing) before the slash UI reflects changes.
- **`only_me` flag on `/download`** propagates from the cog into the job payload and is honoured by `deliver()` (forces DM-only, fail-closed). The cog's initial `context.defer(ephemeral=only_me)` and ack `context.send(..., ephemeral=only_me)` must also respect it.
- **Idempotent delivery** is enforced by a Redis SET-NX claim in `deliver()` keyed `delivered:{job_id}` with 24h TTL. Anything called downstream of that claim is "at most once per job_id". If you add a new delivery path, route it through `deliver()` rather than calling `user.send`/`channel.send` directly.
- **Tests mirror the package layout** under `tests/` — a change to `downloader_bot/worker/jobs.py` belongs in `tests/worker/test_jobs.py`. Reuse fixtures from the cross-cutting `tests/conftest.py` and per-layer `conftest.py`s rather than re-mocking from scratch. `pyproject.toml` sets `asyncio_mode = "auto"`, so `async def test_*` works without `@pytest.mark.asyncio`. The cross-cutting `tests/conftest.py` sets required env vars (`TOKEN`, `AZURE_CONN_STR`, `POSTGRES_DSN`, etc.) at module-body time, *before* `downloader_bot.*` is imported, because `downloader_bot/config.py` constructs the `settings` singleton at import — per-test `monkeypatch.setenv` is too late.
