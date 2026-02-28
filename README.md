# Downloader Bot

Download all media in a Discord channel

## Env File

This program uses a .env file to load various configuration options.

Copy the `.env.example` to a file named `.env` to get started.

`ENVIRONMENT` - `prod` or `dev`

`LOGGING_LEVEL` - The logging level to output. This is usually `INFO` for `prod` and `DEBUG` for `dev`.

`TOKEN` - Your discord bot token.

`PREFIX` - The prefix used by normal commands in the bot.

`INVITE_LINK` - Your invite link for the bot.

`ALLOWED_MEDIA_TYPES` - A list of allowed media types.

`ST_INT_URL` - The internal URL of the Azure Storage Account.

`ST_EXT_URL` - The external URL of the Azure Storage Account.

`ST_CONTAINER` - The Azure Storage Account container name.

`ST_CONN_STR` - The Azure Storage Account connecion string.

## Docker

### Build

#### Windows

```cmd
docker build -t downloader-bot:<VERSION> .
```

### Raspberry Pi

```cmd
docker build --platform linux/arm64/v8 -t downloader-bot:<VERSION>-arm64-v8 .
```
