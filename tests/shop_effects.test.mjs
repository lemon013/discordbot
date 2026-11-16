import test from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { verifyShopSnapshot, applyJoinEffects, effectsForPlayer } from '../Minecraft-Discord-Bridge/scripts/shopEffects.js';

const secret = 'test-shop-secret-'.repeat(3);
const signed = data => {
  const payload = JSON.stringify(data);
  return JSON.stringify({ payload, signature: createHmac('sha256', secret).update(payload).digest('hex') });
};
const sample = () => ({ version: 1, guild_id: '1', sent_at: 1000, active: { '10': { record: 'record_camera', welcome: 'welcome_wild' } } });

test('accepts signed catalog IDs; rejects tampering, stale data, wrong guild and commands', () => {
  const raw = signed(sample());
  assert.equal(verifyShopSnapshot(raw, secret, '1', 0, 2000).active['10'].record, 'record_camera');
  assert.throws(() => verifyShopSnapshot(raw.replace('record_camera', 'record_levelup'), secret, '1', 0, 2000));
  assert.throws(() => verifyShopSnapshot(raw, secret, '2', 0, 2000));
  assert.throws(() => verifyShopSnapshot(raw, secret, '1', 1000, 2000));
  const data = sample();
  data.active['10'].record = 'random.screenshot @a; op evil';
  assert.throws(() => verifyShopSnapshot(signed(data), secret, '1', 0, 2000));
});

test('uses linked Discord account and case-insensitive Minecraft name', () => {
  const accounts = [{ mcid: 'Player Name', 'discord-id': '10' }];
  const active = sample().active;
  assert.equal(effectsForPlayer(accounts, 'PLAYER NAME', active), active['10']);
  assert.equal(effectsForPlayer(accounts, 'Someone Else', active), undefined);
  assert.equal(effectsForPlayer([], 'Player Name', active), undefined);
});

test('plays screenshot to every listener and renders literal player name safely', async () => {
  const commands = [], messages = [];
  const world = { async runCommand(command) { commands.push(command); }, async sendMessage(text) { messages.push(text); } };
  await applyJoinEffects(world, 'Player "name"', sample().active['10']);
  assert.deepEqual(commands, ['execute as @a at @s run playsound random.screenshot @s ~ ~ ~ 1 1 1']);
  assert.deepEqual(messages, ['あ！野生のPlayer "name"だ！']);
});

test('unknown effects execute nothing; welcome still sends when sound fails', async () => {
  let calls = 0, sent = 0;
  const world = { async runCommand() { calls++; throw new Error('offline'); }, async sendMessage() { sent++; } };
  await applyJoinEffects(world, 'Player', { record: 'unknown', welcome: 'unknown' });
  assert.equal(calls, 0);
  await assert.rejects(applyJoinEffects(world, 'Player', sample().active['10']));
  assert.equal(sent, 1);
});

test('all twelve requested records use the exact provided sound ID', async () => {
  const catalog = JSON.parse(readFileSync(new URL('../Discord-GameBot/shop_catalog.json', import.meta.url), 'utf8'));
  const sounds = ['wind_charge.burst', 'raid.horn', 'random.totem', 'horn.call.0', 'horn.call.1', 'random.hurt',
    'mob.blaze.shoot', 'mob.pig.death', 'mob.ghast.scream', 'mob.enderdragon.hit', 'mob.warden.sonic_boom', 'mob.warden.roar'];
  for (const sound of sounds) {
    const item = catalog.find(item => item.kind === 'record' && item.sound === sound);
    assert.ok(item, sound);
    const commands = [];
    await applyJoinEffects({ async runCommand(command) { commands.push(command); } }, 'Player', { record: item.id });
    assert.deepEqual(commands, [`execute as @a at @s run playsound ${sound} @s ~ ~ ~ 1 1 1`]);
  }
});

test('all ten requested welcomes insert the player name and remove placeholder notation', async () => {
  const catalog = JSON.parse(readFileSync(new URL('../Discord-GameBot/shop_catalog.json', import.meta.url), 'utf8'));
  const expected = [
    'Playerがやってきました',
    'Playerさん、ようこそ。',
    'やあ、Player君。ピザ持ってきたよね？',
    'Playerがただいま着陸いたしました。',
    'Playerが出たぞー！',
    'やったー、Playerが来たー！',
    'いらっしゃいPlayerちゃん。ほら、ちゃんとご挨拶して！',
    'Playerがワールドに滑り込みました。',
    'Playerがパーティに加わりました。',
    'Player、お会いできて何よりです。',
  ];
  for (const text of expected) {
    const item = catalog.find(item => item.kind === 'welcome' && item.text.replaceAll('<プレイヤーネーム>', 'Player') === text);
    assert.ok(item, text);
    const messages = [];
    await applyJoinEffects({ async sendMessage(message) { messages.push(message); } }, 'Player', { welcome: item.id });
    assert.deepEqual(messages, [text]);
  }
});
