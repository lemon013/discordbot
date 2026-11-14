import { existsSync, mkdirSync, readFileSync, writeFileSync, renameSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { deferInteraction } from './interaction.js';

const ALLOWED_USER_ID = '799820074990698496';
const ACCOUNT_FILE = resolve(process.cwd(), '.discord-mcbe', 'account.json');
const DATA_FILE = resolve(process.cwd(), '.discord-mcbe', 'data.json');

function readJson(file, fallback) {
  if (!existsSync(file)) return fallback;
  const raw = readFileSync(file, 'utf8').trim();
  return raw ? JSON.parse(raw) : fallback;
}

function writeJson(file, value) {
  mkdirSync(dirname(file), { recursive: true });
  const temp = `${file}.tmp`;
  writeFileSync(temp, `${JSON.stringify(value, null, 2)}\n`, 'utf8');
  renameSync(temp, file);
}

function isAccountRecord(value) {
  return value && typeof value === 'object' && !Array.isArray(value) &&
    (typeof value['discord-id'] === 'string' || typeof value.discordId === 'string');
}
function recordId(value) {
  return String(value?.['discord-id'] ?? value?.discordId ?? '').replace(/[^0-9]/g, '');
}
function normalizeRecord(value) {
  return {
    mcid: String(value?.mcid ?? value?.name ?? ''),
    'discord-name': String(value?.['discord-name'] ?? value?.discordName ?? ''),
    'discord-id': recordId(value),
  };
}

// data.json の旧形式(custom_link:links)なども安全に探索し、対象ユーザーの連携だけ除去する。
function removeMatchingRecords(node, targetId, found) {
  if (Array.isArray(node)) {
    const kept = [];
    for (const item of node) {
      if (isAccountRecord(item) && recordId(item) === targetId) found.push(normalizeRecord(item));
      else kept.push(removeMatchingRecords(item, targetId, found));
    }
    return kept;
  }
  if (!node || typeof node !== 'object') return node;

  // 旧形式は byDiscordId[ID] / byMcid[mcid] で保存されていた。
  if (node.byDiscordId && typeof node.byDiscordId === 'object' && !Array.isArray(node.byDiscordId)) {
    const byDiscordId = { ...node.byDiscordId };
    const old = byDiscordId[targetId];
    if (old && typeof old === 'object') {
      found.push(normalizeRecord({ ...old, discordId: old.discordId ?? targetId }));
      delete byDiscordId[targetId];
    }
    node = { ...node, byDiscordId };
  }

  const result = {};
  for (const [key, value] of Object.entries(node)) {
    if (key === 'byMcid' && value && typeof value === 'object' && !Array.isArray(value)) {
      const byMcid = {};
      for (const [mcid, entry] of Object.entries(value)) {
        if (isAccountRecord(entry) && recordId(entry) === targetId) found.push(normalizeRecord(entry));
        else if (entry && typeof entry === 'object' && String(entry.discordId ?? entry['discord-id'] ?? '').replace(/[^0-9]/g, '') === targetId) {
          found.push(normalizeRecord({ ...entry, mcid: entry.mcid ?? mcid }));
        } else byMcid[mcid] = removeMatchingRecords(entry, targetId, found);
      }
      result[key] = byMcid;
    } else if (isAccountRecord(value) && recordId(value) === targetId) {
      found.push(normalizeRecord(value));
    } else {
      result[key] = removeMatchingRecords(value, targetId, found);
    }
  }
  return result;
}

export const deleteCommandData = {
  name: 'delete',
  description: '連携済みアカウントを削除します（管理者専用）',
  options: [{
    name: 'userid',
    description: '削除するDiscordユーザーID（数字のみ）',
    type: 3,
    required: true,
    min_length: 17,
    max_length: 20,
  }],
};

export async function handleDeleteInteraction(app, interaction) {
  if (!await deferInteraction(interaction, { ephemeral: true })) return;

  if (interaction.user.id !== ALLOWED_USER_ID) {
    await interaction.editReply({ content: '⚠️あなたには必要な権限がありません。' });
    return;
  }

  const targetId = interaction.options.getString('userid', true).trim();
  if (!/^\d{17,20}$/.test(targetId)) {
    await interaction.editReply({ content: '⚠️ユーザーIDは数字のみで入力してください。' });
    return;
  }

  try {
    const found = [];
    const accounts = readJson(ACCOUNT_FILE, []);
    if (!Array.isArray(accounts)) throw new Error('account.json の形式がJSON配列ではありません。');
    const keptAccounts = accounts.filter((entry) => {
      if (isAccountRecord(entry) && recordId(entry) === targetId) {
        found.push(normalizeRecord(entry));
        return false;
      }
      return true;
    });
    if (keptAccounts.length !== accounts.length) writeJson(ACCOUNT_FILE, keptAccounts);

    // 互換性のため data.json 内に旧形式の連携データがあれば、それも削除する。
    if (existsSync(DATA_FILE)) {
      const data = readJson(DATA_FILE, {});
      const cleaned = removeMatchingRecords(data, targetId, found);
      if (JSON.stringify(cleaned) !== JSON.stringify(data)) writeJson(DATA_FILE, cleaned);
    }

    const record = found.find((item) => item['discord-id'] === targetId && (item.mcid || item['discord-name']));
    if (!record) {
      await interaction.editReply({ content: `⚠️ユーザーID ${targetId} に一致する連携データが見つかりませんでした。` });
      return;
    }

    // 同一データが旧形式と新形式の両方にある場合は重複をまとめて返信。
    await interaction.editReply({ content: `✅削除が完了しました！\n削除した人の情報\nmc nametag:${record.mcid}\ndisc name:${record['discord-name']}\ndisc id:${record['discord-id']}` });
    app.logger.info(`[Custom] /delete removed account link for Discord ID ${targetId}`);
  } catch (error) {
    app.logger.error('[Custom] /delete failed:', error);
    await interaction.editReply({ content: '⚠️削除中にエラーが発生しました。コンソールのログを確認してください。' });
  }
}
