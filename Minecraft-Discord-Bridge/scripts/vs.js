import { deferInteraction } from './interaction.js';
import { findGameScores, refreshPlayerStatusSnapshots } from './playerStatus.js';

const WORLDS = {
  MINIGAMER: { key: 'mini_neo', metrics: [['rocal', 'RocalCoin']] },
  'MurderMysteryRe:MASTER': { key: 'MurderMystery', metrics: [['Murder', '殺人鬼陣営になった回数'], ['Crew', '村人陣営になった回数'], ['wingame', '勝利数']] },
  unlimited: { key: 'unlimited', metrics: [['up', 'Level'], ['wingame', '勝利数'], ['killCount', 'キル数'], ['deathCount', 'デス数']] },
};

export async function handleVsInteraction(app, interaction) {
  if (!await deferInteraction(interaction)) return;
  const name = interaction.options.getString('name')?.trim();
  const opponent = interaction.options.getString('opponent')?.trim();
  const worldName = interaction.options.getString('world');
  const world = Object.hasOwn(WORLDS, worldName) ? WORLDS[worldName] : null;
  const reply = (content) => interaction.editReply({ content, allowedMentions: { parse: [] } });
  if (!name || !opponent || name.length > 64 || opponent.length > 64) {
    await reply('⚠️ 自身と相手のMinecraft名を1～64文字で入力してください。'); return;
  }
  if (!world) { await reply('⚠️ ワールドを選択肢から選んでください。'); return; }
  if (name.toLowerCase() === opponent.toLowerCase()) {
    await reply('⚠️ 自身とは異なる対戦相手を指定してください。'); return;
  }
  await refreshPlayerStatusSnapshots(app);
  const left = findGameScores(app, name, world.key);
  const right = findGameScores(app, opponent, world.key);
  const safe = (text) => String(text).replace(/[\\`*_~|<>\[\]\r\n]/g, '');
  const missing = [left, right].filter((status) => status.scores.size === 0);
  if (missing.length) {
    await reply(`${missing.map((status) => safe(status.name)).join('、')} の ${worldName} の保存データがありません。`);
    return;
  }
  const lines = [`**${safe(left.name)} VS ${safe(right.name)}**`, `ワールド: ${worldName}`, '表示順: 自身 / 相手 （差 = 自身 − 相手）', ''];
  for (const [key, label] of world.metrics) {
    const a = left.scores.get(key), b = right.scores.get(key);
    const hasA = Number.isFinite(a), hasB = Number.isFinite(b);
    const delta = hasA && hasB ? a - b : null;
    lines.push(`**${label}**: ${hasA ? a : '未記録'} / ${hasB ? b : '未記録'} （差: ${delta === null ? '比較不可' : `${delta > 0 ? '+' : ''}${delta}`}）`);
  }
  lines.push('', '保存済みデータによる比較です。各項目の差は勝敗を表しません。');
  for (const status of [left, right]) {
    if (status.updatedAt) lines.push(`${safe(status.name)} の更新: <t:${Math.floor(status.updatedAt / 1000)}:R>`);
  }
  await reply(lines.join('\n'));
}
