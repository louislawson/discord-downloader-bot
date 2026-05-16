"""Downloader Bot to download media from a Discord Channel."""

import os
import platform

import asyncpg
import discord
import discordhealthcheck
from discord.ext import commands, tasks
from discord.ext.commands import Context, errors

from downloader_bot.config import settings
from downloader_bot.db.guild_settings import GuildSettingsRepo
from downloader_bot.db.pool import build_pool, close_pool
from downloader_bot.embeds import error
from downloader_bot.logging_setup import init_logger
from downloader_bot.presence import STATUSES, cycle_random
from downloader_bot.tq import broker

intents = discord.Intents.default()
intents.message_content = True


logger = init_logger("downloader_bot")


class DiscordBot(commands.Bot):
    """Custom Discord bot wrapping ``commands.Bot``.

    Centralises cog auto-loading, lifecycle wiring (broker / pool / repo /
    healthcheck / status loop), and command-error handling.
    """

    def __init__(self) -> None:
        """Initialise the DiscordBot."""
        super().__init__(
            command_prefix=commands.when_mentioned_or(settings.PREFIX),
            intents=intents,
            help_command=None,
        )
        self.logger = logger
        self.bot_prefix = settings.PREFIX
        self.invite_link = settings.INVITE_LINK
        self.healthcheck_server = None
        self.db_pool: asyncpg.Pool | None = None
        self.guild_settings_repo: GuildSettingsRepo | None = None
        self._status_picker = cycle_random(STATUSES)

    async def load_cogs(self) -> None:
        """Load all cog extensions from the /cogs directory."""
        for file in os.listdir(f"{os.path.realpath(os.path.dirname(__file__))}/cogs"):
            if file.endswith(".py") and not file.startswith("_"):
                extension = file[:-3]
                try:
                    await self.load_extension(f"downloader_bot.cogs.{extension}")
                    self.logger.info("Loaded extension '%s'", extension)
                except errors.ExtensionNotFound as e:
                    self.logger.exception(
                        "Couldn't find extension '%s': %s", extension, e
                    )
                except errors.ExtensionAlreadyLoaded as e:
                    self.logger.exception(
                        "Extension already loaded '%s': %s", extension, e
                    )
                except errors.NoEntryPointError as e:
                    self.logger.exception(
                        "Extension has no setup() entry point '%s': %s", extension, e
                    )
                except errors.ExtensionFailed as e:
                    self.logger.exception(
                        "Extension '%s' raised an error during load: %s", extension, e
                    )

    @tasks.loop(minutes=1.0)
    async def status_task(self) -> None:
        """Cycle the bot's presence status."""
        await self.change_presence(activity=discord.Game(next(self._status_picker)))

    @status_task.before_loop
    async def before_status_task(self) -> None:
        """Wait until the bot is ready before starting the status loop."""
        await self.wait_until_ready()

    async def setup_hook(self) -> None:
        """Initialise broker, DB pool, repo, cogs, healthcheck and status loop.

        Runs once after login. Order matters: the broker has to start before
        any cog can enqueue tasks, and the pool / repo have to exist before
        any cog can read guild settings. ``broker.startup()`` is skipped on
        the worker side via ``broker.is_worker_process``.
        """
        self.logger.info("Logged in as %s", self.user.name)
        self.logger.info("discord.py API version: %s", discord.__version__)
        self.logger.info("Python version: %s", platform.python_version())
        self.logger.info(
            "Running on: %s %s (%s)", platform.system(), platform.release(), os.name
        )
        self.logger.info("-------------------")
        if not broker.is_worker_process:
            await broker.startup()
        self.db_pool = await build_pool()
        self.guild_settings_repo = GuildSettingsRepo(self.db_pool)
        await self.load_cogs()
        self.healthcheck_server = await discordhealthcheck.start(self)
        self.logger.info("Connected to Redis at %s", settings.REDIS_URL)
        self.status_task.start()

    async def close(self):
        """Shut down healthcheck, broker, DB pool, then the discord.py client.

        Mirror of :meth:`setup_hook`. ``broker.shutdown()`` is skipped on the
        worker side (the worker owns its own broker lifecycle).
        """
        if self.healthcheck_server is not None:
            await self.healthcheck_server.wait_closed()
        if not broker.is_worker_process:
            await broker.shutdown()
        if self.db_pool is not None:
            await close_pool(self.db_pool)
        await super().close()

    # pylint: disable=arguments-differ
    async def on_message(self, message: discord.Message) -> None:
        """Process commands from non-bot users.

        Args:
            message: The message that was sent.
        """
        if message.author == self.user or message.author.bot:
            return
        await self.process_commands(message)

    async def on_command_completion(self, context: Context) -> None:
        """Log successfully executed commands.

        Args:
            context: The context of the command.
        """
        executed_command = context.command.qualified_name.split(" ")[0]
        if context.guild is not None:
            self.logger.info(
                "Executed '%s' in '%s' (ID: %s) by %s (ID: %s)",
                executed_command,
                context.guild.name,
                context.guild.id,
                context.author,
                context.author.id,
            )
        else:
            self.logger.info(
                "Executed '%s' by %s (ID: %s) in DMs",
                executed_command,
                context.author,
                context.author.id,
            )

    async def on_command_error(
        self,
        context: Context,
        cmd_error: errors.CommandError,
    ) -> None:
        """Translate known command errors into user-facing embeds.

        Known error types (cooldown, missing perms, bad argument, etc.) get
        a tailored embed; anything unexpected is logged with a traceback and
        the user gets a generic "unexpected error" embed so internals don't
        leak. ``CommandNotFound`` is silently ignored.

        Args:
            context: The context of the command.
            cmd_error: The error that was raised.
        """
        if isinstance(cmd_error, commands.CommandOnCooldown):
            minutes, seconds = divmod(cmd_error.retry_after, 60)
            hours, minutes = divmod(minutes, 60)
            hours = hours % 24
            parts = []
            if round(hours) > 0:
                parts.append(f"{round(hours)} hours")
            if round(minutes) > 0:
                parts.append(f"{round(minutes)} minutes")
            if round(seconds) > 0:
                parts.append(f"{round(seconds)} seconds")
            embed = error(
                title="Error",
                description=f"**Please slow down** — you can use this command again in {', '.join(parts)}.",
            )
            await context.send(embed=embed)

        elif isinstance(cmd_error, commands.NotOwner):
            embed = error(
                title="Error",
                description="You are not the owner of the bot!",
            )
            await context.send(embed=embed)
            if context.guild:
                self.logger.warning(
                    "%s (ID: %s) tried to execute an owner-only command in '%s' (ID: %s).",
                    context.author,
                    context.author.id,
                    context.guild.name,
                    context.guild.id,
                )
            else:
                self.logger.warning(
                    "%s (ID: %s) tried to execute an owner-only command in DMs.",
                    context.author,
                    context.author.id,
                )

        elif isinstance(cmd_error, commands.MissingPermissions):
            embed = error(
                title="Error",
                description=(
                    "You are missing the permission(s) `"
                    + ", ".join(cmd_error.missing_permissions)
                    + "` to execute this command!"
                ),
            )
            await context.send(embed=embed)

        elif isinstance(cmd_error, commands.BotMissingPermissions):
            self.logger.warning(
                "Bot is missing permissions %s to run '%s' in channel '%s'.",
                cmd_error.missing_permissions,
                context.command,
                context.channel,
            )
            embed = error(
                title="Error",
                description=(
                    "I am missing the permission(s) `"
                    + ", ".join(cmd_error.missing_permissions)
                    + "` to fully perform this command!"
                ),
            )
            await context.send(embed=embed)

        elif isinstance(cmd_error, commands.MissingRequiredArgument):
            embed = error(
                title="Missing argument",
                description=str(cmd_error).capitalize(),
            )
            await context.send(embed=embed)

        elif isinstance(cmd_error, commands.BadArgument):
            embed = error(
                title="Invalid argument",
                description=str(cmd_error).capitalize(),
            )
            await context.send(embed=embed)

        elif isinstance(cmd_error, commands.MaxConcurrencyReached):
            embed = error(
                title="Error",
                description=(
                    "This command is already running in this channel. "
                    "Please wait for it to finish before running it again."
                ),
            )
            await context.send(embed=embed)

        elif isinstance(cmd_error, commands.CommandNotFound):
            # Silently ignore unknown commands — no need to log or respond.
            return

        else:
            # Genuinely unexpected — log the full traceback and let the user
            # know something went wrong without exposing internal details.
            self.logger.exception(
                "Unhandled error in command '%s' invoked by %s (ID: %s): %s",
                context.command,
                context.author,
                context.author.id,
                cmd_error,
            )
            embed = error(
                title="Unexpected error",
                description=(
                    "An unexpected error occurred while running this command. "
                    "Please try again later, or contact an administrator if this keeps happening."
                ),
            )
            await context.send(embed=embed)


def main() -> None:
    """Construct the bot and hand control to discord.py's gateway loop."""
    bot = DiscordBot()
    bot.run(settings.TOKEN)


if __name__ == "__main__":
    main()
