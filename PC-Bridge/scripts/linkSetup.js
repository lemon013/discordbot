// Reuse the original linking flow, but run it entirely on the PC Bot.
import { setupLinkChat, handleLinkInteraction, linkCommandData } from '../../Minecraft-Discord-Bridge/scripts/link.js';
import { isAcknowledgementError } from '../../Minecraft-Discord-Bridge/scripts/interaction.js';
import { handleDeleteInteraction, deleteCommandData } from '../../Minecraft-Discord-Bridge/scripts/delete.js';

const CLOUD_COMMANDS = new Set(['player', 'vs', 'wordwolfex', '8192game']);

async function cleanPCCommands(app, guild) {
  for (const command of (await guild.commands.fetch()).values()) {
    if (CLOUD_COMMANDS.has(command.name)) {
      await command.delete();
      app.logger.info(`[PC Commands] Removed cloud-only /${command.name} from PC Bot`);
    }
  }
  // Bridge registers its built-ins and custom commands at guild scope.
  // Remove old global copies, including commands left by a previous combined Bot.
  const guildNames = new Set([...((await guild.commands.fetch()).values())].map(c => c.name));
  for (const command of (await app.bot.client.application.commands.fetch()).values()) {
    if (guildNames.has(command.name) || CLOUD_COMMANDS.has(command.name) || ['link', 'delete'].includes(command.name)) {
      await command.delete();
      app.logger.info(`[PC Commands] Removed obsolete global /${command.name} from PC Bot`);
    }
  }
}

export async function setupPCLink(app) {
  if (!app.__pcLinkChatReady) {
    setupLinkChat(app);
    app.__pcLinkChatReady = true;
  }

  const client = app.bot.client;
  if (app.__pcLinkListener) client.removeListener('interactionCreate', app.__pcLinkListener);
  const handled = app.__pcLinkHandled ??= new Set();
  const listener = async interaction => {
    if (!interaction.isChatInputCommand?.() || !['link', 'delete'].includes(interaction.commandName) ||
        interaction.guildId !== app.env.GUILD_ID || handled.has(interaction.id)) return;
    handled.add(interaction.id);
    setTimeout(() => handled.delete(interaction.id), 15 * 60 * 1000).unref?.();
    try {
      if (interaction.commandName === 'link') await handleLinkInteraction(app, interaction);
      else await handleDeleteInteraction(app, interaction);
    } catch (error) {
      if (isAcknowledgementError(error)) return;
      app.logger.error('[PC Link] Failed to link account', error);
      try {
        const message = { content: 'アカウント連携に失敗しました。PC側のログを確認してください。', embeds: [] };
        if (interaction.deferred || interaction.replied) await interaction.editReply(message);
        else await interaction.reply({ ...message, flags: 64 });
      } catch (replyError) {
        if (!isAcknowledgementError(replyError)) app.logger.error('[PC Link] Failed to send error response', replyError);
      }
    }
  };
  app.__pcLinkListener = listener;
  client.on('interactionCreate', listener);

  await app.bot.awaitReady();
  const guild = client.guilds.cache.get(app.env.GUILD_ID) ?? await client.guilds.fetch(app.env.GUILD_ID);
  // Creating a same-name guild command updates it; never replace the full command list.
  await guild.commands.create(linkCommandData);
  await guild.commands.create(deleteCommandData);
  await cleanPCCommands(app, guild);
  app.logger.info('[PC Link] Minecraft !link and PC Bot /link are ready');
}
