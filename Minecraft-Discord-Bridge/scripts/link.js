import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { generateLinkCode, levenshteinDistance } from './util.js';
import { deferInteraction } from './interaction.js';

// start.bat はツール本体のルートで起動するため、
// discord-mcbe-windows-x64-v8\\.discord-mcbe\\account.json に対応する。
const ACCOUNT_FILE = resolve(process.cwd(), '.discord-mcbe', 'account.json');

// 有効期限: 5分
const CODE_EXPIRE_MS = 5 * 60 * 1000;
// クールダウン: 5分
const COOLDOWN_MS = 5 * 60 * 1000;

/**
 * @typedef {{
 *   code: string,
 *   mcid: string,
 *   createdAt: number,
 *   expiresAt: number,
 * }} PendingLink
 */

/** @type {Map<string, PendingLink>} コード -> 保留データ */
const pendingCodes = new Map();

/** @type {Map<string, PendingLink>} MCID(小文字) -> 保留データ */
const pendingByMcid = new Map();

/** @type {Map<string, number>} MCID(小文字) -> 最終発行タイムスタンプ */
const lastIssuedTimestamps = new Map();

/**
 * 直近に失効・使用されたコードの履歴（打ち間違いや期限切れ検知用）
 * @type {Map<string, { mcid: string, expiredAt: number, reason: 'expired' | 'used' }>}
 */
const recentHistory = new Map();

function ensureAccountFile() {
  mkdirSync(dirname(ACCOUNT_FILE), { recursive: true });
  try {
    readFileSync(ACCOUNT_FILE, 'utf8');
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error;
    writeFileSync(ACCOUNT_FILE, '[]\n', 'utf8');
  }
}

/**
 * @returns {Array<{mcid: string, 'discord-name': string, 'discord-id': string}>}
 */
function loadAccounts() {
  ensureAccountFile();

  const raw = readFileSync(ACCOUNT_FILE, 'utf8').trim();
  if (!raw) return [];

  const parsed = JSON.parse(raw);
  if (!Array.isArray(parsed)) {
    throw new Error(`${ACCOUNT_FILE} はJSON配列である必要があります。`);
  }

  return parsed.filter((account) =>
    account &&
    typeof account.mcid === 'string' &&
    typeof account['discord-name'] === 'string' &&
    typeof account['discord-id'] === 'string'
  );
}

/**
 * JSONを壊しにくくするため、一時ファイル経由で置き換える。
 * @param {Array<{mcid: string, 'discord-name': string, 'discord-id': string}>} accounts
 */
function saveAccounts(accounts) {
  ensureAccountFile();
  const tempFile = `${ACCOUNT_FILE}.tmp`;
  writeFileSync(tempFile, `${JSON.stringify(accounts, null, 2)}\n`, 'utf8');
  renameSync(tempFile, ACCOUNT_FILE);
}

/**
 * Discordユーザー名を指定された保存形式に合わせる。
 * アルファベット(A-Z/a-z)、数字、_ だけを残す。
 * @param {string} username
 */
function sanitizeDiscordName(username) {
  return String(username ?? '').replace(/[^A-Za-z0-9_]/g, '');
}

/**
 * アカウント連携情報を保存する。
 * MC側・Discord側とも1アカウント1連携になるよう、既存の関連付けを置き換える。
 *
 * 保存形式:
 * [
 *   {
 *     "mcid": "Minecraftの純粋なネームタグ",
 *     "discord-name": "Discordユーザー名(英数字と_のみ)",
 *     "discord-id": "DiscordユーザーID"
 *   }
 * ]
 *
 * @param {import('@discord-mcbe/server').Application} app
 * @param {string} mcid
 * @param {string} discordId
 * @param {string} discordName
 */
export function saveLink(app, mcid, discordId, discordName) {
  const cleanMcid = mcid.trim();
  const cleanDiscordId = String(discordId).replace(/[^0-9]/g, '');
  const cleanDiscordName = sanitizeDiscordName(discordName);

  if (!cleanMcid) throw new Error('Minecraftのネームタグが空です。');
  if (!/^\d+$/.test(cleanDiscordId)) throw new Error('DiscordユーザーIDが不正です。');
  if (!cleanDiscordName) throw new Error('Discordユーザー名から保存可能な文字がありません。');

  const mcidKey = cleanMcid.toLowerCase();
  let accounts = loadAccounts();

  // 同じMinecraft名、または同じDiscord IDの古い連携を除外。
  accounts = accounts.filter((account) =>
    account.mcid.toLowerCase() !== mcidKey && account['discord-id'] !== cleanDiscordId
  );

  accounts.push({
    mcid: cleanMcid,
    'discord-name': cleanDiscordName,
    'discord-id': cleanDiscordId,
  });

  saveAccounts(accounts);
}

/**
 * Discord IDからMinecraftのネームタグを取得する。
 * @param {import('@discord-mcbe/server').Application} app
 * @param {string} discordId
 * @returns {string | undefined}
 */
export function getMcidByDiscordId(app, discordId) {
  const cleanDiscordId = String(discordId).replace(/[^0-9]/g, '');
  const account = loadAccounts().find((entry) => entry['discord-id'] === cleanDiscordId);
  return account?.mcid;
}

/**
 * 保存先を確認するための補助関数。
 * @returns {string}
 */
export function getAccountFilePath() {
  return ACCOUNT_FILE;
}

/**
 * 旧バージョンの app.properties に残っている連携情報を初回起動時だけ移行する。
 * account.json に既にデータがある場合は上書きしない。
 * @param {import('@discord-mcbe/server').Application} app
 */
function migrateLegacyLinks(app) {
  const accounts = loadAccounts();
  if (accounts.length > 0) return;

  const legacyKey = 'custom_link:links';
  const legacy = app.properties.get(legacyKey);
  if (!legacy || typeof legacy !== 'object' || !legacy.byMcid || typeof legacy.byMcid !== 'object') {
    return;
  }

  const migrated = [];
  for (const entry of Object.values(legacy.byMcid)) {
    if (!entry || typeof entry !== 'object') continue;
    const mcid = typeof entry.mcid === 'string' ? entry.mcid.trim() : '';
    const discordId = String(entry.discordId ?? '').replace(/[^0-9]/g, '');
    const discordName = sanitizeDiscordName(entry.discordName ?? '');
    if (!mcid || !/^\d+$/.test(discordId) || !discordName) continue;

    if (migrated.some((item) => item.mcid.toLowerCase() === mcid.toLowerCase() || item['discord-id'] === discordId)) {
      continue;
    }

    migrated.push({
      mcid,
      'discord-name': discordName,
      'discord-id': discordId,
    });
  }

  if (migrated.length > 0) {
    saveAccounts(migrated);
    app.logger.info(`[Link] 旧アカウント連携データ ${migrated.length} 件を account.json に移行しました`);
  }
}

/**
 * 期限切れになったコードを定期的に整理する
 */
function cleanupExpiredCodes() {
  const now = Date.now();
  for (const [code, entry] of pendingCodes.entries()) {
    if (now >= entry.expiresAt) {
      pendingCodes.delete(code);
      pendingByMcid.delete(entry.mcid.toLowerCase());
      recentHistory.set(code, { mcid: entry.mcid, expiredAt: now, reason: 'expired' });
    }
  }

  // 15分以上前の履歴は破棄
  for (const [code, hist] of recentHistory.entries()) {
    if (now - hist.expiredAt > 15 * 60 * 1000) {
      recentHistory.delete(code);
    }
  }
}

/**
 * 接続中のワールドからオンラインプレイヤーを検索する
 * @param {import('@discord-mcbe/server').Application} app
 * @param {string} mcid
 */
function findOnlinePlayer(app, mcid) {
  const clean = mcid.toLowerCase();
  for (const world of app.minecraft?.getWorlds?.() ?? []) {
    const player = world.getPlayers?.().find((p) => p.name.toLowerCase() === clean);
    if (player) return player;
  }
  return undefined;
}

/**
 * マインクラフト側でタグ「yumokiti」が付いているプレイヤーにお知らせを送信する
 * ※パケットの競合やアドオンのクラッシュを防ぐため、遅延処理(setTimeout)かつ安全に実行する
 * @param {import('@discord-mcbe/server').Application} app
 * @param {string} playerName
 */
function notifyYumokitiAdmins(app, playerName) {
  setTimeout(async () => {
    try {
      const worlds = app.minecraft?.getWorlds?.() ?? [];
      for (const world of worlds) {
        // 1. オンラインプレイヤーの中に名前に yumokiti が含まれる人がいれば直接送信
        const players = world.getPlayers?.() ?? [];
        for (const p of players) {
          if (p.name.toLowerCase().includes('yumokiti')) {
            p.sendMessage(`§6[管理通知] §e${playerName}§f が §b!link§f を実行しました。`).catch(() => {});
          }
        }

        // 2. BDS ネイティブ接続(WebSocket)から安全に tellraw @a[tag=yumokiti] を送信
        const nativeWorld = world.session?.world;
        if (nativeWorld?.runCommand) {
          const rawtext = JSON.stringify({
            rawtext: [
              { text: `§6[管理通知] §e${playerName}§f が §b!link§f を実行しました。` },
            ],
          });
          await nativeWorld.runCommand(`tellraw @a[tag=yumokiti] ${rawtext}`).catch(() => {});
        }
      }
    } catch (e) {
      app.logger.debug?.(`[Link] yumokiti通知実行エラー (安全に無視): ${e?.message ?? e}`);
    }
  }, 500);
}

/**
 * "!link" チャットを検知して8桁コードを本人にだけ通知し、Discordへの転送をキャンセルする。
 * 重要: app.before のフックは必ず同期関数にし、return false で確実にチャットをキャンセルする。
 * @param {import('@discord-mcbe/server').Application} app
 */
export function setupLinkChat(app) {
  // 起動時に account.json を作成しておく。
  try {
    ensureAccountFile();
    migrateLegacyLinks(app);
    app.logger.info(`[Link] アカウント連携データ: ${ACCOUNT_FILE}`);
  } catch (e) {
    app.logger.error('[Link] account.json の初期化に失敗しました:', e);
  }

  // 1分ごとに期限切れコードをクリーンアップ
  setInterval(() => cleanupExpiredCodes(), 60 * 1000);

  // 同期関数として登録 (async にすると return false が Promise になりキャンセル判定をすり抜ける)
  app.before('minecraftMessage', (event) => {
    if (event.message.trim().toLowerCase() !== '!link') return true;

    const player = event.sender;
    const mcidLower = player.name.toLowerCase();
    const now = Date.now();

    cleanupExpiredCodes();

    // 5分間のクールダウン確認
    const lastIssued = lastIssuedTimestamps.get(mcidLower);
    if (lastIssued && now - lastIssued < COOLDOWN_MS) {
      const remainingSec = Math.ceil((COOLDOWN_MS - (now - lastIssued)) / 1000);
      const min = Math.floor(remainingSec / 60);
      const sec = remainingSec % 60;
      const timeStr = min > 0 ? `${min}分${sec}秒` : `${sec}秒`;

      player.sendMessage(
        `§c[エラー] コード再発行は5分間のクールダウンが必要です(残り: ${timeStr})`
      ).catch((e) => app.logger.error('[Link]', e));

      return false;
    }

    // 古い保留コードがあれば削除
    const oldPending = pendingByMcid.get(mcidLower);
    if (oldPending) {
      pendingCodes.delete(oldPending.code);
      recentHistory.set(oldPending.code, { mcid: player.name, expiredAt: now, reason: 'expired' });
    }

    // 重複しない8桁コードを生成
    let code;
    let attempts = 0;
    do {
      code = generateLinkCode();
      attempts++;
    } while (pendingCodes.has(code) && attempts < 10);

    const pendingEntry = {
      code,
      mcid: player.name,
      createdAt: now,
      expiresAt: now + CODE_EXPIRE_MS,
    };

    pendingCodes.set(code, pendingEntry);
    pendingByMcid.set(mcidLower, pendingEntry);
    lastIssuedTimestamps.set(mcidLower, now);

    // プレイヤー本人にのみ通知
    // クライアント切断/クラッシュ防止のため、空行を挟まず安全な長さ・文字で送信
    player.sendMessage([
      `§f連携コードは: §e§l${code}§rです。`,
      `§rDiscordで §l§b/link code:${code} §rを実行してください。`,
      '§c他人には教えず、5分以内に入力してください。',
    ].join('\n')).catch((e) => app.logger.error('[Link] コード送信失敗:', e));

    app.logger.info(`[Link] Issued 8-digit code for ${player.name}`);

    // 管理者（tag=yumokiti）へ通知（0.5秒遅延で安全に実行）
    notifyYumokitiAdmins(app, player.name);

    return false;
  });
}

/**
 * Discordの /link code インタラクションを処理する。
 * @param {import('@discord-mcbe/server').Application} app
 * @param {import('discord.js').ChatInputCommandInteraction} interaction
 */
export async function handleLinkInteraction(app, interaction) {
  // 先にInteractionをACKしておき、以降の処理が遅れても10062にならないようにする。
  if (!await deferInteraction(interaction, { ephemeral: true })) return;

  cleanupExpiredCodes();

  const code = interaction.options.getString('code', true).trim();
  const entry = pendingCodes.get(code);

  if (!entry) {
    // 入力エラー時の警告対象プレイヤーを特定
    const targetMcids = new Set();

    // 1. 直近の履歴（期限切れまたは使用済みコード）に存在するか確認
    if (recentHistory.has(code)) {
      targetMcids.add(recentHistory.get(code).mcid);
    }

    // 2. 現在有効な保留中コードとの類似度（打ち間違い検知）
    let closestEntry = null;
    let minDistance = Infinity;
    for (const pending of pendingCodes.values()) {
      const dist = levenshteinDistance(code, pending.code);
      if (dist < minDistance) {
        minDistance = dist;
        closestEntry = pending;
      }
    }

    // 2文字以内のミスであればタイポと判定
    if (closestEntry && minDistance <= 2) {
      targetMcids.add(closestEntry.mcid);
    }

    // 3. 特定できない場合でも、現在コードを発行して待機しているプレイヤーがいれば警告対象とする
    if (targetMcids.size === 0) {
      for (const pending of pendingCodes.values()) {
        targetMcids.add(pending.mcid);
      }
    }

    // マインクラフト側でコードを発行した本人にだけ警告チャットを送信 (安全な短文)
    for (const mcid of targetMcids) {
      const targetPlayer = findOnlinePlayer(app, mcid);
      if (targetPlayer) {
        targetPlayer.sendMessage([
          '§c==============================',
          '§c[警告] Discordで誤った連携コードが入力されました',
          '§e※心当たりがない場合は他人が推測を試みた可能性があります',
          '§c==============================',
        ].join('\n')).catch((e) => app.logger.error('[Link] 警告送信エラー:', e));
      }
    }

    await interaction.editReply({
      embeds: [{
        color: 0xd32f2f,
        author: { name: '連携失敗' },
        description: '⚠️連携コードが無効か、有効期限が切れています。\nマインクラフト内でもう一度 `!link` と発言して、発行された8桁のコードを正確に入力してください。',
      }],
    });
    return;
  }

  // 認証成功: アカウント連携を保存
  const cleanName = entry.mcid.trim();
  saveLink(app, cleanName, interaction.user.id, interaction.user.username);

  // 【一度限り】使用したコードを即座に破棄
  pendingCodes.delete(code);
  pendingByMcid.delete(cleanName.toLowerCase());
  recentHistory.set(code, { mcid: cleanName, expiredAt: Date.now(), reason: 'used' });

  await interaction.editReply({
    embeds: [{
      color: 0x2ecc71,
      author: { name: '連携完了' },
      description: `✅ Minecraftアカウント **${cleanName}** と連携しました。`,
    }],
  });

  // マインクラフト側の本人にも成功通知
  const onlinePlayer = findOnlinePlayer(app, cleanName);
  if (onlinePlayer) {
    onlinePlayer.sendMessage([
      `§a[連携完了] Discord (@${interaction.user.tag}) と連携しました！`,
      '§l§aWelcome to ぐりーんの鯖！',
    ].join('\n')).catch(() => {});
  }

  app.logger.info(`[Link] ${interaction.user.tag} linked to ${cleanName}`);
}

export const linkCommandData = {
  name: 'link',
  description: 'Minecraftのアカウントと連携します',
  options: [
    {
      name: 'code',
      description: 'マインクラフト内で !link を実行すると表示される8桁のコード',
      type: 3, // STRING
      required: true,
      min_length: 8,
      max_length: 8,
    },
  ],
};
