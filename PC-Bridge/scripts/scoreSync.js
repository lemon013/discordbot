export const REGISTRY = 'custom_player_status:saved_scoreboards';
export const SNAPSHOTS = 'custom_player_status:snapshots';
export const TOMBSTONES = 'custom_player_status:deleted_scoreboards';
export const offlineName = 'commands.scoreboard.players.offlineplayername';

export function isKnownPlayerName(value) {
  const name = String(value ?? '').trim();
  return !!name && name.toLowerCase() !== offlineName && !/^commands\.scoreboard\.players\./i.test(name);
}

export function mergePlayers(previous, observed) {
  const merged = {};
  for (const [key, player] of Object.entries(previous ?? {})) {
    if (player && isKnownPlayerName(player.name) && typeof player.score === 'number' && Number.isFinite(player.score)) {
      merged[key] = {name: String(player.name).trim(), score: player.score};
    }
  }
  for (const [key, player] of Object.entries(observed ?? {})) {
    if (!player || !isKnownPlayerName(player.name) || typeof player.score !== 'number' || !Number.isFinite(player.score)) continue;
    merged[player.name.toLowerCase()] = {name: player.name.trim(), score: player.score};
  }
  return merged;
}

export function deletionKey(world, board) { return `${world}\u001f${board}`; }

export function deleteScoreboardData(properties, world, board, timestamp = Date.now()) {
  const snapshots = properties.get(SNAPSHOTS) ?? {};
  const worldData = snapshots[world];
  if (worldData?.scoreboards) delete worldData.scoreboards[board];
  if (worldData && Object.keys(worldData.scoreboards ?? {}).length === 0) delete snapshots[world];
  properties.set(SNAPSHOTS, snapshots);
  const tombstones = properties.get(TOMBSTONES) ?? {};
  tombstones[deletionKey(world, board)] = Math.max(tombstones[deletionKey(world, board)] ?? 0, timestamp);
  properties.set(TOMBSTONES, tombstones);
}

export function collectScoreboard(previous, scores, updatedAt = Date.now()) {
  if (!Array.isArray(scores)) return previous;
  const observed = {};
  for (const info of scores) {
    const name = String(info.participant?.displayName ?? '').trim();
    const score = info.score;
    if (isKnownPlayerName(name) && typeof score === 'number' && Number.isFinite(score)) {
      observed[name.toLowerCase()] = {name, score};
    }
  }
  return {updatedAt, players: mergePlayers(previous?.players, observed)};
}

