import { createHmac, timingSafeEqual } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { getAccountFilePath } from './link.js';

const CATALOG = JSON.parse(readFileSync(new URL('../../Discord-GameBot/shop_catalog.json', import.meta.url), 'utf8'));
const ITEMS = new Map(CATALOG.map(item => [item.id, item]));
const KEY = 'jewel_shop:active';
const STAMP = 'jewel_shop:stamp';
const MAX_BYTES = 2_000_000;

export function verifyShopSnapshot(raw, secret, guildId, previous = 0, now = Date.now()) {
  if (Buffer.byteLength(raw) > MAX_BYTES || secret.length < 32) throw new Error('Invalid shop envelope');
  const envelope = JSON.parse(raw);
  if (typeof envelope.payload !== 'string' || !/^[a-f0-9]{64}$/.test(envelope.signature ?? '')) throw new Error('Invalid signature');
  const expected = createHmac('sha256', secret).update(envelope.payload).digest();
  if (!timingSafeEqual(expected, Buffer.from(envelope.signature, 'hex'))) throw new Error('Invalid signature');
  const data = JSON.parse(envelope.payload);
  if (data.version !== 1 || data.guild_id !== String(guildId) || !Number.isSafeInteger(data.sent_at) ||
      data.sent_at <= previous || data.sent_at > now + 300_000 || !data.active || Array.isArray(data.active) ||
      typeof data.active !== 'object' || Object.keys(data.active).length > 10000) throw new Error('Invalid snapshot');
  for (const [userId, active] of Object.entries(data.active)) {
    if (!/^\d{1,20}$/.test(userId) || !active || typeof active !== 'object' || Array.isArray(active)) throw new Error('Invalid user');
    for (const kind of ['record', 'welcome']) {
      if (active[kind] !== null && active[kind] !== undefined && ITEMS.get(active[kind])?.kind !== kind) throw new Error('Invalid item');
    }
  }
  return data;
}

export async function applyJoinEffects(world, playerName, active) {
  const record = ITEMS.get(active?.record);
  const welcome = ITEMS.get(active?.welcome);
  const actions = [];
  if (record?.kind === 'record' && /^[a-z0-9_.]+$/.test(record.sound)) {
    // Execute at each listener so the whole world hears the same sound, regardless of distance.
    actions.push(world.runCommand(`execute as @a at @s run playsound ${record.sound} @s ~ ~ ~ 1 1 1`));
  }
  if (welcome?.kind === 'welcome') {
    const text = welcome.text.replaceAll('<プレイヤーネーム>', playerName);
    actions.push(world.sendMessage(text));
  }
  await Promise.all(actions);
}

export function effectsForPlayer(accounts, playerName, active) {
  if (!Array.isArray(accounts)) return undefined;
  const account = accounts.find(account => typeof account?.mcid === 'string' && account.mcid.toLowerCase() === playerName.toLowerCase());
  if (!account || !/^\d+$/.test(account['discord-id'] ?? '')) return undefined;
  return active?.[account['discord-id']];
}

export function setupShopEffects(app) {
  if (app.__shopEffects) return;
  const secret = process.env.MC_SYNC_SECRET ?? '';
  const channelId = process.env.MC_SYNC_CHANNEL_ID ?? '';
  const guildId = app.env.GUILD_ID;
  const senderId = process.env.MC_SHOP_SENDER_ID ?? '';
  const client = app.bot.client;
  if (secret.length < 32 || !/^\d+$/.test(channelId ?? '')) {
    app.logger.info('[Shop] Join effects wait for MC_SYNC_CHANNEL_ID and MC_SYNC_SECRET');
    return;
  }
  app.__shopEffects = true;
  let reading = false;
  const consume = async message => {
    if (message.channelId !== channelId || !message.author?.bot || (senderId && message.author.id !== senderId)) return;
    for (const attachment of message.attachments.values()) {
      if (attachment.name !== 'shop-entitlements.json' || attachment.size > MAX_BYTES) continue;
      try {
        const url = new URL(attachment.url);
        if (url.protocol !== 'https:' || !['cdn.discordapp.com', 'media.discordapp.net'].includes(url.hostname)) continue;
        const response = await fetch(url, { signal: AbortSignal.timeout(10000) });
        if (!response.ok) continue;
        const raw = await response.text();
        const data = verifyShopSnapshot(raw, secret, guildId, app.properties.get(STAMP) ?? 0);
        app.properties.set(KEY, data.active);
        app.properties.set(STAMP, data.sent_at);
        app.logger.info(`[Shop] Join effects synchronized: ${Object.keys(data.active).length} players`);
      } catch {
        // Invalid signatures, old snapshots, or unavailable attachments never execute commands.
        app.logger.debug?.('[Shop] Ignored invalid, old, or unavailable shop snapshot');
      }
    }
  };
  client.on('messageCreate', consume);
  const refresh = async () => {
    if (reading) return;
    reading = true;
    try {
      await app.bot.awaitReady();
      const channel = client.channels.cache.get(channelId) ?? await client.channels.fetch(channelId);
      if (channel.guildId !== guildId) throw new Error('Shop sync channel belongs to another guild');
      const messages = await channel.messages.fetch({ limit: 50 });
      for (const message of messages.values()) await consume(message);
    } catch {
      app.logger.warn('[Shop] Cannot read shop sync channel; check permissions and channel ID');
    } finally {
      reading = false;
    }
  };
  void refresh();
  setInterval(refresh, 60000).unref?.();
  const recent = new Map();
  app.on('playerJoin', async event => {
    try {
      const playerName = event.player.name;
      const key = `${event.world.name}\u0000${playerName.toLowerCase()}`;
      if (Date.now() - (recent.get(key) ?? 0) < 10000) return;
      recent.set(key, Date.now());
      if (recent.size > 1024) recent.delete(recent.keys().next().value);
      const accounts = JSON.parse(readFileSync(getAccountFilePath(), 'utf8').replace(/^\uFEFF/, ''));
      const active = effectsForPlayer(accounts, playerName, app.properties.get(KEY) ?? {});
      if (active) await applyJoinEffects(event.world, playerName, active);
    } catch {
      app.logger.warn('[Shop] Could not apply join effects; check account link and Minecraft command permissions');
    }
  });
}
