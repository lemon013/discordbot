import { handleVsInteraction } from './vs.js';
import { setupLinkChat, handleLinkInteraction, getMcidByDiscordId, linkCommandData } from './link.js';
import { setupPlayerStatusChat, startPlayerStatusPolling, handlePlayerStatusInteraction, playerCommandData } from './playerStatus.js';
import { handleDeleteInteraction, deleteCommandData } from './delete.js';
import { isAcknowledgementError } from './interaction.js';

const CHAT_INPUT_TYPE = 1;

/**
 * @param {import('@discord-mcbe/server').Application} app
 * @param {Record<string, any>} commandData
 */
async function syncGuildCommand(app, commandData) {
  const guild = app.bot.client.guilds.cache.get(app.env.GUILD_ID);
  if (!guild) throw new Error(`Guild not found: ${app.env.GUILD_ID}`);

  // Discord.js のキャッシュだけで判断すると、既にDiscord側から消えた
  // コマンドIDを DELETE/EDIT して 10063 になることがあるため、必ず最新状態を取得する。
  const commands = await guild.commands.fetch({ force: true });
  const sameName = [...commands.values()].filter((command) =>
    command.type === CHAT_INPUT_TYPE && command.name === commandData.name
  );

  const isUnknownCommand = (error) => error?.code === 10063 || error?.rawError?.code === 10063;

  // 同名コマンドが複数残っている場合だけ整理する。
  // 「既に存在しないID」を削除しようとしても 10063 でプロセスを落とさない。
  if (commandData.name === 'link' && sameName.length > 1) {
    // 最新の1件を残し、その他を削除する。
    const keep = sameName[0];
    for (const oldCommand of sameName.slice(1)) {
      try {
        await oldCommand.delete();
        app.logger.info(`[Custom] 旧 /link (ID: ${oldCommand.id}) を削除しました`);
      } catch (error) {
        if (isUnknownCommand(error)) {
          app.logger.warn(`[Custom] 旧 /link (ID: ${oldCommand.id}) は既に存在しないため削除をスキップしました`);
        } else {
          throw error;
        }
      }
    }

    try {
      const updated = await keep.edit(commandData);
      app.logger.info(`[Custom] /link を更新しました (ID: ${updated.id})`);
      return updated;
    } catch (error) {
      // Discord側で直前に削除された等、IDが無効になっていた場合は新規作成へフォールバック。
      if (!isUnknownCommand(error)) throw error;
    }
  }

  // 同名の1件があれば既存コマンドを更新。存在しなくなった場合は新規作成。
  const existing = sameName[0];
  if (existing) {
    try {
      const updated = await existing.edit(commandData);
      app.logger.info(`[Custom] /${commandData.name} を更新しました (ID: ${updated.id})`);
      return updated;
    } catch (error) {
      if (!isUnknownCommand(error)) throw error;
      app.logger.warn(`[Custom] /${commandData.name} (ID: ${existing.id}) が存在しないため新規登録します`);
    }
  }

  const created = await guild.commands.create(commandData);
  app.logger.info(`[Custom] /${commandData.name} を登録しました (ID: ${created.id})`);
  return created;
}

/**
 * @param {import('@discord-mcbe/server').Application} app
 */
export default async function main(app) {
  app.logger.info('[Custom] /link, /player status, /delete を読み込み中...');

  // Minecraft側の !link、保存設定コマンド、ステータススナップショットを起動。
  setupLinkChat(app);
  setupPlayerStatusChat(app);
  startPlayerStatusPolling(app);

  // Listenerはコマンド同期前に登録。受信したInteractionは各ハンドラが即deferReply()する。
  // A custom script can be reloaded while the Discord client stays alive.
  // Handle each interaction ID once so two listeners cannot both defer it.
  const handledInteractions = app.__customHandledInteractions ??= new Set();
  app.bot.client.on('interactionCreate', async (interaction) => {
    if (!interaction.isChatInputCommand?.()) return;

    const isLink = interaction.commandName === 'link';
    const isPlayerStatus = interaction.commandName === 'player' &&
      interaction.options.getSubcommand(false) === 'status';
    const isDelete = interaction.commandName === 'delete';
    const isVs = interaction.commandName === 'vs';
    if (!isLink && !isPlayerStatus && !isDelete && !isVs) return;
    if (handledInteractions.has(interaction.id)) return;
    handledInteractions.add(interaction.id);
    setTimeout(() => handledInteractions.delete(interaction.id), 15 * 60 * 1000);

    try {
      app.logger.debug(`[Custom] Interaction received: /${interaction.commandName}`);
      if (isLink) {
        await handleLinkInteraction(app, interaction);
      } else if (isVs) {
        await handleVsInteraction(app, interaction);
      } else if (isPlayerStatus) {
        await handlePlayerStatusInteraction(app, interaction, getMcidByDiscordId);
      } else {
        await handleDeleteInteraction(app, interaction);
      }
    } catch (e) {
      if (isAcknowledgementError(e)) {
        app.logger.warn(`[Custom] /${interaction.commandName} was already acknowledged; skipping duplicate response`);
        return;
      }
      app.logger.error('[Custom] Failed to handle interaction:', e);
      try {
        if (interaction.deferred || interaction.replied) {
          await interaction.editReply({
            content: '⚠️コマンドの処理中にエラーが発生しました。コンソールのログを確認してください。',
            embeds: [],
          });
        } else {
          await interaction.reply({
            content: '⚠️コマンドの処理中にエラーが発生しました。コンソールのログを確認してください。',
            flags: isLink ? 64 : undefined,
          });
        }
      } catch (replyError) {
        if (!isAcknowledgementError(replyError)) {
          app.logger.error('[Custom] Failed to send interaction error reply:', replyError);
        }
      }
    }
  });

  await app.bot.awaitReady();

  // 同一Bot構成では Discord-GameBot が全コマンドを一括登録する。
  // ここでも登録すると、両プロセスの同期が互いのコマンドを消してしまう。
  app.logger.info('[Custom] Bridge interaction handlers are ready; command registration is managed by Discord-GameBot');
}
