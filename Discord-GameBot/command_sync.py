"""Publish GameBot commands globally and refresh the optional test guild."""
import logging

log = logging.getLogger(__name__)


async def sync_cloud_commands(bot, test_guild_id=None):
    import discord
    managed = {command.name for command in bot.tree.get_commands()} | {'link', 'delete', 'give'}
    target = discord.Object(id=int(test_guild_id)) if test_guild_id else None
    global_commands = await bot.tree.sync()
    log.info('Synced %s global GameBot commands', len(global_commands))
    if target:
        bot.tree.copy_global_to(guild=target)
        guild_commands = await bot.tree.sync(guild=target)
        log.info('Synced %s GameBot commands in test guild %s', len(guild_commands), target.id)

    # Global and guild commands can leave duplicate entries after changing modes.
    # Remove only this project's commands from the other scopes.
    scopes = {guild.id: guild for guild in bot.guilds}
    if target:
        scopes[target.id] = target
    for guild in scopes.values():
        try:
            commands = await bot.tree.fetch_commands(guild=guild)
            for command in commands:
                obsolete = command.name in {'link', 'delete', 'give'}
                other_scope = target is None or guild.id != target.id
                if obsolete or (other_scope and command.name in managed):
                    await command.delete()
                    log.info('Removed duplicate/obsolete cloud command /%s (scope=%s)',
                             command.name, guild.id if guild else 'global')
        except Exception:
            log.exception('Could not clean cloud command scope %s', guild.id if guild else 'global')
    log.info('Cloud commands ready: games, /player status and /vs; account linking belongs to PC Bot')
