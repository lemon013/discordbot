// Reuse the original linking flow, but run it entirely on the PC Bot.
import { setupLinkChat, handleLinkInteraction, linkCommandData } from '../../Minecraft-Discord-Bridge/scripts/link.js';
import { isAcknowledgementError } from '../../Minecraft-Discord-Bridge/scripts/interaction.js';

export async function setupPCLink(app) {
  if (!app.__pcLinkChatReady) {
    setupLinkChat(app);
    app.__pcLinkChatReady = true;
  }

  const client = app.bot.client;
  if (app.__pcLinkListener) client.removeListener('interactionCreate', app.__pcLinkListener);
  const handled = app.__pcLinkHandled ??= new Set();
  const listener = async interaction => {
    if (!interaction.isChatInputCommand?.() || interaction.commandName !== 'link' ||
        interaction.guildId !== app.env.GUILD_ID || handled.has(interaction.id)) return;
    handled.add(interaction.id);
    setTimeout(() => handled.delete(interaction.id), 15 * 60 * 1000).unref?.();
    try {
      await handleLinkInteraction(app, interaction);
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
  app.logger.info('[PC Link] Minecraft !link and PC Bot /link are ready');
}
