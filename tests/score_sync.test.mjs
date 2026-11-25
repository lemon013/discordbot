import test from 'node:test';
import assert from 'node:assert/strict';
import { collectScoreboard, deleteScoreboardData, REGISTRY, SNAPSHOTS, TOMBSTONES } from '../PC-Bridge/scripts/scoreSync.js';

test('score collection keeps offline records and ignores placeholder participants', () => {
  const previous = {updatedAt: 1, players: {alice: {name: 'Alice', score: 10}, bob: {name: 'Bob', score: 3},
    'commands.scoreboard.players.offlineplayername': {name: 'commands.scoreboard.players.offlinePlayerName', score: 900}}};
  const next = collectScoreboard(previous, [
    {participant: {displayName: 'Alice'}, score: 12},
    {participant: {displayName: 'commands.scoreboard.players.offlinePlayerName'}, score: 999},
    {participant: {displayName: 'Charlie'}, score: 4},
  ], 2);
  assert.deepEqual(next.players, {
    alice: {name: 'Alice', score: 12},
    bob: {name: 'Bob', score: 3},
    charlie: {name: 'Charlie', score: 4},
  });
});

test('failed or malformed score query leaves the previous scoreboard intact', () => {
  const previous = {updatedAt: 1, players: {alice: {name: 'Alice', score: 10}}};
  assert.equal(collectScoreboard(previous, undefined), previous);
  assert.equal(collectScoreboard(previous, null), previous);
});

test('explicit removal clears saved scoreboard and writes a persistent tombstone', () => {
  const store = new Map([
    [SNAPSHOTS, {World: {scoreboards: {up: {players: {alice: {name: 'Alice', score: 1}}}}}}],
    [TOMBSTONES, {}],
  ]);
  deleteScoreboardData({get: key => store.get(key), set: (key, value) => store.set(key, value)}, 'World', 'up', 123);
  assert.equal(store.get(SNAPSHOTS).World, undefined);
  assert.equal(store.get(TOMBSTONES)['World\u001fup'], 123);
  assert.equal(store.get(REGISTRY), undefined);
});

