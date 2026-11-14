import { deferInteraction } from './interaction.js';

const REGISTRY_KEY = 'custom_player_status:saved_scoreboards';
const SNAPSHOT_KEY = 'custom_player_status:snapshots';
const POLL_INTERVAL_MS = 10_000;
const MANAGER_MC_IDS = new Set(['yumokiti4325', 'greenview777']);
let pollPromise;

/** @param {import('@discord-mcbe/server').Application} app */
function getRegistry(app) {
  const value = app.properties.get(REGISTRY_KEY);
  return Array.isArray(value) ? value.filter((entry) => entry && typeof entry.worldName === 'string' && typeof entry.scoreboard === 'string') : [];
}
/** @param {import('@discord-mcbe/server').Application} app */
function saveRegistry(app, registry) { app.properties.set(REGISTRY_KEY, registry); }
/** @param {import('@discord-mcbe/server').Application} app */
function getSnapshots(app) { const value = app.properties.get(SNAPSHOT_KEY); return value && typeof value === 'object' ? value : {}; }
/** @param {import('@discord-mcbe/server').Application} app */
function saveSnapshots(app, snapshots) { app.properties.set(SNAPSHOT_KEY, snapshots); }
function isManager(player) { return MANAGER_MC_IDS.has(String(player.name ?? '').toLowerCase()); }
function send(player, text) { player.sendMessage(text).catch(() => {}); }

/** Minecraftチャットの保存設定コマンドを登録する。 */
export function setupPlayerStatusChat(app) {
  app.before('minecraftMessage', (event) => {
    const message = event.message.trim();
    if (!message.toLowerCase().startsWith('!save')) return true;
    const player = event.sender;
    if (!isManager(player)) { send(player, '§c[保存設定] このコマンドは yumokiti4325 または GreenView777 のみ実行できます。'); return false; }
    const saveMatch = message.match(/^!savescore\s+([^\s]+)\s*$/i);
    if (saveMatch) {
      const scoreboard = saveMatch[1], worldName = event.world.name, registry = getRegistry(app);
      const gameKey = classifySavedScoreboard(worldName, scoreboard, registry);
      if (registry.some((entry) => entry.worldName === worldName && entry.scoreboard === scoreboard)) send(player, `§e[保存設定] 既に保存済みです: ${worldName} / ${scoreboard}`);
      else { registry.push({ worldName, scoreboard, gameKey, createdAt: Date.now() }); saveRegistry(app, registry); send(player, `§a[保存設定] 保存対象に追加しました: ${worldName} / ${scoreboard}${gameKey ? ` (分類: ${gameKey})` : ' (ゲーム分類なし)'}`); }
      return false;
    }
    if (/^!savelist\s*$/i.test(message)) {
  const registry = getRegistry(app);

  if (registry.length === 0) {
    send(player, '§e[保存設定] 保存中のワールド / スコアボードはありません。');
    return false;
  }

  try {
    send(player, '§a[保存設定] 保存中のワールド / スコアボード');

    for (let i = 0; i < registry.length; i++) {
      const entry = registry[i];

      const line =
        `§f${i + 1}. §b${entry.worldName} §7/ §e${entry.scoreboard}`;

      send(player, line);
    }
  } catch (error) {
    console.error('[保存設定] !savelist エラー:', error);
  }

  return false;
}
    const removeMatch = message.match(/^!saveremove\s+(.+\S)\s+([^\s]+)\s*$/i);
    if (removeMatch) {
      const worldName = removeMatch[1], scoreboard = removeMatch[2], registry = getRegistry(app);
      const next = registry.filter((entry) => !(entry.worldName === worldName && entry.scoreboard === scoreboard));
      if (next.length === registry.length) send(player, `§e[保存設定] 見つかりません: ${worldName} / ${scoreboard}`);
      else {
        saveRegistry(app, next);
        const snapshots = getSnapshots(app);
        if (snapshots[worldName]?.scoreboards) { delete snapshots[worldName].scoreboards[scoreboard]; snapshots[worldName].updatedAt = Date.now(); saveSnapshots(app, snapshots); }
        send(player, `§a[保存設定] 削除しました: ${worldName} / ${scoreboard}`);
      }
      return false;
    }
    send(player, '§e使い方: !savescore <scoreboard> / !savelist / !saveremove <world> <scoreboard>');
    return false;
  });
}

/** 保存対象の全ワールド/スコアボードを10秒ごとに永続スナップショットへ更新する。 */
export function startPlayerStatusPolling(app) {
  const poll = () => pollOnce(app).catch((error) => app.logger.error('[PlayerStatus]', error));
  poll(); setInterval(poll, POLL_INTERVAL_MS);
}
/**
 * Coalesce periodic and command-triggered refreshes.  A status command waits
 * for the same refresh already in progress instead of reading half-old data.
 */
export function refreshPlayerStatusSnapshots(app) {
  return pollOnce(app);
}
function pollOnce(app) {
  if (pollPromise) return pollPromise;
  pollPromise = (async () => {
    const registry = getRegistry(app); if (registry.length === 0) return;
    const snapshots = getSnapshots(app);
    for (const world of app.minecraft.getWorlds()) {
      const targets = registry.filter((entry) => entry.worldName === world.name); if (targets.length === 0) continue;
      const worldSnapshot = snapshots[world.name] ?? { worldName: world.name, scoreboards: {} };
      worldSnapshot.scoreboards ??= {}; let changed = false;
      for (const target of targets) {
        const objective = await world.scoreboard.getObjective(target.scoreboard).catch(() => undefined); if (!objective) continue;
        const scores = await objective.getScores().catch(() => undefined); if (!scores) continue;
        const players = {};
        for (const info of scores) { const name = String(info.participant?.displayName ?? '').trim(); if (name) players[name.toLowerCase()] = { name, score: info.score }; }
        worldSnapshot.scoreboards[target.scoreboard] = { updatedAt: Date.now(), players }; changed = true;
      }
      if (changed) { worldSnapshot.updatedAt = Date.now(); snapshots[world.name] = worldSnapshot; }
    }
    saveSnapshots(app, snapshots);
  })().finally(() => { pollPromise = undefined; });
  return pollPromise;
}

const GAME_SCOREBOARDS = {
  unlimited: ['up', 'wingame', 'killCount', 'deathCount'],
  MurderMystery: ['Murder', 'Crew', 'wingame'],
  mini_neo: ['rocal'],
};
function classifySavedScoreboard(worldName, scoreboard, registry = []) {
  const objective = String(scoreboard).toLowerCase();
  const world = String(worldName).toLowerCase();
  // MINIGAMER_NEO は同じワールド内の rocal など関連スコアボードにも適用する。
  if (objective.includes('minigamer_neo') || registry.some((entry) =>
    entry.worldName === worldName && String(entry.scoreboard).toLowerCase().includes('minigamer_neo'))) return 'mini_neo';
  if (world.includes('murdermystery') || world.includes('人狼')) return 'MurderMystery';
  if (world.includes('minigamer') && !world.includes('unlimited')) return 'mini_neo';
  if (world.includes('unlimited')) return 'unlimited';
  if (world.includes('人狼')) return 'MurderMystery';
  return null;
}
function getEntryGameKey(entry, registry) {
  return entry.gameKey || classifySavedScoreboard(entry.worldName, entry.scoreboard, registry);
}
export function findGameScores(app, name, gameKey) {
  const key = name.trim().toLowerCase(), registry = getRegistry(app), snapshots = getSnapshots(app);
  const scores = new Map(); let displayName = name.trim(), updatedAt = 0;
  for (const target of registry) {
    if (getEntryGameKey(target, registry) !== gameKey) continue;
    const snapshot = snapshots[target.worldName]?.scoreboards?.[target.scoreboard];
    const participant = snapshot?.players?.[key];
    if (!participant) continue;
    const scoreboardName = target.scoreboard.toLowerCase();
    // MINIGAMER_NEO 自体がCoin値を保持する構成と、rocal objectiveを別登録する構成の両方に対応。
    const objectiveKey = gameKey === 'mini_neo'
      ? (scoreboardName === 'rocal' || scoreboardName.includes('minigamer_neo') ? 'rocal' : null)
      : GAME_SCOREBOARDS[gameKey].find((item) => item.toLowerCase() === scoreboardName);
    // 対象ゲームで表示する objective のみを採用する。
    if (!objectiveKey) continue;
    if (objectiveKey !== 'rocal' || !scores.has('rocal') || scoreboardName === 'rocal') scores.set(objectiveKey, participant.score);
    displayName = participant.name || displayName;
    updatedAt = Math.max(updatedAt, Number(snapshot.updatedAt) || 0);
  }
  return { name: displayName, scores, updatedAt };
}
function formatGameStatus(status, gameKey) {
  const value = (key) => status.scores.has(key) ? status.scores.get(key) : 0;
  const lines = [`${status.name}さんのステータスは`];
  if (gameKey === 'unlimited') lines.push(`💪${value('up')}Level | 🏆${value('wingame')} Win | ⚔️${value('killCount')} Kills | 💀${value('deathCount')} Deaths`);
  else if (gameKey === 'MurderMystery') lines.push(`🔪殺人鬼陣営になった回数: ${value('Murder')} | 🛠️乗組員陣営になった回数: ${value('Crew')} | 🏆勝利数 ${value('wingame')}`);
  else lines.push(`🪙${value('rocal')}RocalCoin`);
  if (status.updatedAt) lines.push(`更新: <t:${Math.floor(status.updatedAt / 1000)}:R>`);
  return lines.join('\n');
}

export const playerCommandData = { name: 'player', description: '保存済みのプレイヤー情報を表示します', options: [{ name: 'status', description: 'ゲームを選択してプレイヤーのステータスを表示します', type: 1, options: [
  { name: 'game', description: '参照するゲーム', type: 3, required: true, choices: [
    { name: 'unlimited', value: 'unlimited' },
    { name: 'MurderMysteryRe:MASTER', value: 'MurderMysteryRe:MASTER' },
    { name: 'MINIGAMER', value: 'MINIGAMER' },
  ] },
  { name: 'name', description: 'プレイヤー名', type: 3, required: true, max_length: 64 },
] }] };

export async function handlePlayerStatusInteraction(app, interaction) {
  if (!await deferInteraction(interaction)) return;
  const gameChoice = interaction.options.getString('game');
  const name = interaction.options.getString('name')?.trim();
  if (!name) { await interaction.editReply('⚠️プレイヤー名を指定してください。'); return; }
  const gameKey = gameChoice === 'unlimited' ? 'unlimited' : gameChoice === 'MurderMysteryRe:MASTER' ? 'MurderMystery' : gameChoice === 'MINIGAMER' ? 'mini_neo' : null;
  if (!gameKey) { await interaction.editReply('⚠️ゲームの指定が正しくありません。'); return; }
  const registry = getRegistry(app);
  if (!registry.some((entry) => getEntryGameKey(entry, registry) === gameKey)) {
    await interaction.editReply('⚠️このゲームの保存対象がありません。Minecraftで対象のスコアボードに `!savescore <scoreboard>` を実行してください。'); return;
  }
  await refreshPlayerStatusSnapshots(app);
  const status = findGameScores(app, name, gameKey);
  if (status.scores.size === 0) { await interaction.editReply('⚠️保存済みスナップショットにこのプレイヤーのデータがありません。対象ワールドに接続してから、もう一度実行してください。'); return; }
  await interaction.editReply({ content: formatGameStatus(status, gameKey) });
}
