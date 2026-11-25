import { setupPCLink } from './linkSetup.js';
import { setupShopEffects } from '../../Minecraft-Discord-Bridge/scripts/shopEffects.js';
import { REGISTRY, SNAPSHOTS, TOMBSTONES, collectScoreboard, deleteScoreboardData } from './scoreSync.js';
const managers = new Set(['yumokiti4325', 'greenview777', 'niwatori0815']);

function classify(world, board, registry) {
  const name = world.toLowerCase();
  if (board.toLowerCase().includes('minigamer_neo') || registry.some(e =>
    e.worldName === world && e.scoreboard.toLowerCase().includes('minigamer_neo'))) return 'mini_neo';
  if (name.includes('unlimited')) return 'unlimited';
  if (name.includes('murdermystery') || name.includes('人狼')) return 'MurderMystery';
  if (name.includes('minigamer')) return 'mini_neo';
  const boards = new Set([board.toLowerCase(), ...registry.filter(e => e.worldName === world).map(e => e.scoreboard.toLowerCase())]);
  if (boards.has('murder') && boards.has('crew')) return 'MurderMystery';
  return null;
}

export default async function main(app) {
  setupShopEffects(app);
  const registry = () => app.properties.get(REGISTRY) ?? [];
  app.before('minecraftMessage', event => {
    const message = event.message.trim();
    if (!message.toLowerCase().startsWith('!save') && !/^!scoreremove(?:\s|$)/i.test(message)) return true;
    const send = text => event.sender.sendMessage(text).catch(() => {});
    if (!managers.has(event.sender.name.toLowerCase())) {
      send('§cこの操作を実行する権限がありません。'); return false;
    }
    const match = message.match(/^!savescore\s+(\S+)\s*$/i);
    if (match) {
      const entries = registry(), worldName = event.world.name, scoreboard = match[1];
      if (!entries.some(e => e.worldName === worldName && e.scoreboard === scoreboard)) {
        entries.push({worldName, scoreboard, gameKey: classify(worldName, scoreboard, entries), createdAt: Date.now()});
        app.properties.set(REGISTRY, entries);
      }
      send(`§a保存対象: ${worldName} / ${scoreboard}`); return false;
    }
    if (/^!savelist\s*$/i.test(message)) {
      send(registry().map(e => `${e.worldName} / ${e.scoreboard}`).join('\n') || '保存対象はありません。');
      return false;
    }
    const remove = message.match(/^!(?:saveremove|scoreremove)\s+(.+\S)\s+(\S+)\s*$/i);
    if (remove) {
      const worldName = remove[1], scoreboard = remove[2];
      const current = registry();
      const next = current.filter(e => e.worldName !== worldName || e.scoreboard !== scoreboard);
      if (next.length === current.length) { send(`No saved scoreboard: ${worldName} / ${scoreboard}`); return false; }
      deleteScoreboardData(app.properties, worldName, scoreboard);
      app.properties.set(REGISTRY, next);
      send('§a保存対象を更新しました。'); return false;
    }
    send('!savescore <scoreboard> / !savelist / !saveremove <world> <scoreboard> / !scoreremove <world> <scoreboard>');
    return false;
  });
  let polling = false;
  const poll = async () => {
    if (polling) return;
    polling = true;
    try {
      const entries = registry(), snapshots = app.properties.get(SNAPSHOTS) ?? {};
      let reclassified = false;
      for (const entry of entries) {
        if (!entry.gameKey) {
          const gameKey = classify(entry.worldName, entry.scoreboard, entries);
          if (gameKey) { entry.gameKey = gameKey; reclassified = true; }
        }
      }
      if (reclassified) app.properties.set(REGISTRY, entries);
      for (const world of app.minecraft.getWorlds()) {
        const state = snapshots[world.name] ?? {worldName: world.name, scoreboards: {}};
        let changed = false;
        for (const target of entries.filter(e => e.worldName === world.name)) {
          const objective = await world.scoreboard.getObjective(target.scoreboard).catch(() => undefined);
          if (!objective) continue;
          const scores = await objective.getScores().catch(() => undefined);
          if (!scores) continue;
          state.scoreboards[target.scoreboard] = {
            ...collectScoreboard(state.scoreboards[target.scoreboard], scores),
            ...(target.createdAt ? {registeredAt: target.createdAt} : {}),
          };
          changed = true;
        }
        if (changed) { state.updatedAt = Date.now(); snapshots[world.name] = state; }
      }
      if (!app.properties.get(TOMBSTONES)) app.properties.set(TOMBSTONES, {});
      app.properties.set(SNAPSHOTS, snapshots);
    } catch (error) { app.logger.error('[PC score collection]', error); }
    finally { polling = false; }
  };
  await poll();
  setInterval(poll, 10000);
  try {
    await setupPCLink(app);
  } catch (error) {
    app.logger.error('[PC Link] Could not register /link; check GUILD_ID and Bot command permissions', error);
  }
  app.logger.info('[PC] Chat bridge and score collection ready; /link is handled by the PC Bot');
}
