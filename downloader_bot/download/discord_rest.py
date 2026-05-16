"""REST-only Discord client for the Taskiq worker.

The worker never connects to the gateway. ``Client.login(token)`` opens
the authenticated HTTP session without spinning up the gateway, which
is exactly the lifecycle we want for a background worker. Every
operation the worker needs (``fetch_channel``, ``fetch_user``,
``channel.history``, ``Attachment.read``, ``User.send``) is a REST call
served by the same client.
"""

import discord


async def open_client(token: str) -> discord.Client:
    """Open a logged-in Discord client without a gateway connection.

    Pair with :func:`close_client` at worker shutdown.

    Args:
        token: Discord bot token.

    Returns:
        A logged-in ``discord.Client`` whose REST surface is ready for use.
    """
    client = discord.Client(intents=discord.Intents.none())
    await client.login(token)
    return client


async def close_client(client: discord.Client) -> None:
    """Release the underlying HTTP session.

    Args:
        client: The client returned by :func:`open_client`.
    """
    await client.close()
