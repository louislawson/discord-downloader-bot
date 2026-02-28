"""Downloader Bot to download media from a Discord Channel."""

import logging
import os
import platform
import random

import discord
from discord.ext import commands, tasks
from discord.ext.commands import Context, errors
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True


class LoggingFormatter(logging.Formatter):
    """
    Custom logging formatter for discord.py.

    Attributes:
        black (str): black ANSI code.
        red (str): red ANSI code.
        green (str): green ANSI code.
        yellow (str): yellow ANSI code.
        blue (str): blue ANSI code.
        gray (str): gray ANSI code.
        reset (str): reset ANSI code.
        bold (str): bold ANSI code.
        COLORS (Dict[int, str]): Relates logging level to a colour/style.
    """

    # Colors
    black = "\x1b[30m"
    red = "\x1b[31m"
    green = "\x1b[32m"
    yellow = "\x1b[33m"
    blue = "\x1b[34m"
    gray = "\x1b[38m"
    # Styles
    reset = "\x1b[0m"
    bold = "\x1b[1m"

    COLORS = {
        logging.DEBUG: gray + bold,
        logging.INFO: blue + bold,
        logging.WARNING: yellow + bold,
        logging.ERROR: red,
        logging.CRITICAL: red + bold,
    }

    def format(self, record):
        log_color = self.COLORS[record.levelno]
        log_format = "(black){asctime}(reset) (levelcolor){levelname:<8}(reset) (green){name}(reset) {message}"
        log_format = log_format.replace("(black)", self.black + self.bold)
        log_format = log_format.replace("(reset)", self.reset)
        log_format = log_format.replace("(levelcolor)", log_color)
        log_format = log_format.replace("(green)", self.green + self.bold)
        formatter = logging.Formatter(log_format, "%Y-%m-%d %H:%M:%S", style="{")
        return formatter.format(record)


logger = logging.getLogger("discord_bot")
logger.setLevel(os.getenv("LOGGING_LEVEL"))

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(LoggingFormatter())

# Add the handlers
logger.addHandler(console_handler)


class DiscordBot(commands.Bot):
    """
    Custom Discord Bot class.

    This class overrides some of the base function of the discord.py Bot class
    to provide better functionality for cogs, error handling, and logging.

    Attributes:
        logger (Logger): Logger instance for this bot.
        bot_prefix (str): Bot command prefix.
        invite_link (str): Bot invite link.
    """

    def __init__(self) -> None:
        """Initialise the DiscordBot."""
        super().__init__(
            command_prefix=commands.when_mentioned_or(os.getenv("PREFIX")),
            intents=intents,
            help_command=None,
        )
        self.logger = logger
        self.bot_prefix = os.getenv("PREFIX")
        self.invite_link = os.getenv("INVITE_LINK")

    async def load_cogs(self) -> None:
        """
        The code in this function is executed whenever the bot will start.
        """
        for file in os.listdir(f"{os.path.realpath(os.path.dirname(__file__))}/cogs"):
            if file.endswith(".py"):
                extension = file[:-3]
                try:
                    await self.load_extension(f"cogs.{extension}")
                    self.logger.info("Loaded extension '%s'", extension)
                except errors.ExtensionNotFound as e:
                    exception = f"{type(e).__name__}: {e}"
                    self.logger.error(
                        "Couldn't find extension %s\n%s", extension, exception
                    )
                except errors.ExtensionAlreadyLoaded as e:
                    exception = f"{type(e).__name__}: {e}"
                    self.logger.error(
                        "Extension already loaded %s\n%s", extension, exception
                    )
                except errors.NoEntryPointError as e:
                    exception = f"{type(e).__name__}: {e}"
                    self.logger.error(
                        "Extension does not have entry point %s\n%s",
                        extension,
                        exception,
                    )
                except errors.ExtensionFailed as e:
                    exception = f"{type(e).__name__}: {e}"
                    self.logger.error(
                        "Failed to load extension %s\n%s", extension, exception
                    )

    @tasks.loop(minutes=1.0)
    async def status_task(self) -> None:
        """
        Setup the game status task of the bot.
        """
        statuses = ["with you!"]
        await self.change_presence(activity=discord.Game(random.choice(statuses)))

    @status_task.before_loop
    async def before_status_task(self) -> None:
        """
        Before starting the status changing task, we make sure the bot is ready
        """
        await self.wait_until_ready()

    async def setup_hook(self) -> None:
        self.logger.info("Logged in as %s", self.user.name)
        self.logger.info("discord.py API version: %s", discord.__version__)
        self.logger.info("Python version: %s", platform.python_version())
        self.logger.info(
            "Running on: %s %s (%s)", platform.system(), platform.release(), os.name
        )
        self.logger.info("-------------------")
        await self.load_cogs()
        self.status_task.start()

    # pylint: disable=arguments-differ
    async def on_message(self, message: discord.Message) -> None:
        """
        Executed every time someone sends a message.

        Args:
            message (Message): The message that was sent.
        """
        if message.author == self.user or message.author.bot:
            return
        await self.process_commands(message)

    async def on_command_completion(self, context: Context) -> None:
        """
        Executed every time a normal command has been *successfully* executed.

        Args:
            context (Context): The context of the command.
        """
        full_command_name = context.command.qualified_name
        split = full_command_name.split(" ")
        executed_command = str(split[0])
        if context.guild is not None:
            self.logger.info(
                "Executed %s command in %s (ID: %s) by %s (ID: %s)",
                executed_command,
                context.guild.name,
                context.guild.id,
                context.author,
                context.author.id,
            )
        else:
            self.logger.info(
                "Executed %s command by %s (ID: %s) in DMs",
                executed_command,
                context.author,
                context.author.id,
            )

    async def on_command_error(
        self,
        context: Context,
        error: errors.CommandError,
    ) -> None:
        """
        Executed every time a normal valid command catches an error.

        Args:
            context (Context): The context of the command.
            error (CommandError): The error that was raised.
        """
        if isinstance(error, commands.CommandOnCooldown):
            minutes, seconds = divmod(error.retry_after, 60)
            hours, minutes = divmod(minutes, 60)
            hours = hours % 24
            embed = discord.Embed(
                description=f"**Please slow down** - You can use this command again in {f'{round(hours)} hours' if round(hours) > 0 else ''} {f'{round(minutes)} minutes' if round(minutes) > 0 else ''} {f'{round(seconds)} seconds' if round(seconds) > 0 else ''}.",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.NotOwner):
            embed = discord.Embed(
                description="You are not the owner of the bot!", color=0xE02B2B
            )
            await context.send(embed=embed)
            if context.guild:
                self.logger.warning(
                    "%s (ID: %s) tried to execute an owner only command in the guild %s (ID: %s).",
                    context.author,
                    context.author.id,
                    context.guild.name,
                    context.guild.id,
                )
            else:
                self.logger.warning(
                    "%s (ID: %s) tried to execute an owner only command in the bot's DMs.",
                    context.author,
                    context.author.id,
                )
        elif isinstance(error, commands.MissingPermissions):
            embed = discord.Embed(
                description="You are missing the permission(s) `"
                + ", ".join(error.missing_permissions)
                + "` to execute this command!",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.BotMissingPermissions):
            embed = discord.Embed(
                description="I am missing the permission(s) `"
                + ", ".join(error.missing_permissions)
                + "` to fully perform this command!",
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        elif isinstance(error, commands.MissingRequiredArgument):
            embed = discord.Embed(
                title="Error!",
                description=str(error).capitalize(),
                color=0xE02B2B,
            )
            await context.send(embed=embed)
        else:
            raise error


bot = DiscordBot()
bot.run(os.getenv("TOKEN"))
