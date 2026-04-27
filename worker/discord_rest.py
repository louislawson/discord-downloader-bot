"""REST-only Discord client for the worker.

The worker never connects to the gateway — every operation it needs
(``fetch_channel``, ``fetch_user``, ``fetch_guild``, ``channel.history``,
``user.send``, ``channel.send``, and ``Attachment.save``) is a REST call.
``Client.login(token)`` opens the authenticated HTTP session without spinning
up the gateway, which is exactly the lifecycle we want for a background
worker process.
"""

import discord


async def open_client(token: str) -> discord.Client:
    """
    Open a logged-in Discord client with no gateway connection.

    Args:
        token (str): Bot token (the same one the bot process uses).

    Returns:
        discord.Client: An authenticated client suitable for REST calls only.
    """
    client = discord.Client(intents=discord.Intents.none())
    await client.login(token)
    return client
