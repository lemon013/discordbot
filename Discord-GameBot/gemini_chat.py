"""Opt-in Gemini chat. No API requests are made until a user asks a question."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import logging
import os
from pathlib import Path
import re
import time

import aiohttp
import discord

API_ROOT = 'https://generativelanguage.googleapis.com/v1beta'
DEFAULT_PROMPT = 'あなたはDiscordサーバーのアシスタントです。日本語で、親しみやすく正確に回答してください。'
HELP = (
    '**Gemini AI**\n'
    '`./ai chat 質問` または `@Bot 質問`\n'
    '`./ai settings` 現在の設定\n'
    '`./ai models` 利用可能なモデル一覧（無料とは限りません）\n'
    '`./ai reset` このチャンネルの会話履歴を削除（サーバー管理者）\n'
    '**設定変更（サーバー管理者）**\n'
    '`./ai model モデルID`\n'
    '`./ai prompt 指示文`（改行も可）\n'
    '`./ai prompt reset` 初期プロンプトへ戻す\n'
    '`./ai reasoning auto|off|minimal|low|medium|high`\n'
    '思考設定の対応範囲はモデルにより異なります。\n'
    'AIへの質問と直近の会話履歴はGoogleに送信されます。'
)
log = logging.getLogger(__name__)


class AIError(Exception):
    """A message that is safe to show in Discord (never raw API errors)."""


def thinking_config(model: str, level: str) -> dict:
    if level not in ('auto', 'off', 'minimal', 'low', 'medium', 'high'):
        raise AIError('思考レベルは auto / off / minimal / low / medium / high から選んでください。')
    if level == 'auto':
        return {}
    if model.startswith('gemini-2.5-'):
        if level == 'off' and 'pro' in model:
            raise AIError('Gemini 2.5 Proでは思考をoffにできません。auto または low以上を選んでください。')
        budget = {'off': 0, 'minimal': 512, 'low': 1024, 'medium': 4096, 'high': 8192}[level]
        return {'thinkingConfig': {'thinkingBudget': budget}}
    if re.match(r'gemini-[34](?:[.-])', model):
        if level == 'off':
            raise AIError('このモデルではoffを指定できません。auto または対応する思考レベルを選んでください。')
        return {'thinkingConfig': {'thinkingLevel': level.upper()}}
    raise AIError('このモデルの思考設定は未対応です。autoを使用してください。')


def response_text(data: dict) -> tuple[str, dict]:
    candidates = data.get('candidates') or []
    if not candidates:
        raise AIError('回答が返されませんでした。質問を言い換えて試してください。')
    candidate = candidates[0]
    content = candidate.get('content') or {}
    parts = content.get('parts') or []
    answer = '\n'.join(p['text'] for p in parts if p.get('text') and not p.get('thought')).strip()
    if not answer:
        if candidate.get('finishReason') == 'MAX_TOKENS':
            raise AIError('思考中に出力上限に達しました。思考レベルを下げるか質問を短くしてください。')
        raise AIError('回答できませんでした。質問を言い換えて試してください。')
    if candidate.get('finishReason') == 'MAX_TOKENS':
        answer += '\n\n（出力上限に達したため、回答が途中で終了しました）'
    # Keep the complete model content, including thought signatures, for follow-up turns.
    return answer, {'role': 'model', 'parts': parts}


class GeminiChat:
    def __init__(self, bot, settings_path: Path | None = None):
        self.bot = bot
        self.path = settings_path or Path(__file__).with_name('ai_settings.json')
        self.config = {}
        if self.path.exists():
            # Fail visibly instead of silently overwriting a damaged settings file.
            self.config = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(self.config, dict):
                raise ValueError('ai_settings.json must contain an object')
        self.history = OrderedDict()
        self.busy = set()
        self.last_used = OrderedDict()
        self.api_slots = asyncio.Semaphore(3)

    def settings(self, guild_id):
        return {
            'model': os.getenv('GEMINI_MODEL', 'gemini-2.5-flash'),
            'prompt': DEFAULT_PROMPT,
            'reasoning': 'auto',
            **self.config.get(str(guild_id), {}),
        }

    def save(self, guild_id, settings):
        updated = {**self.config, str(guild_id): settings}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)
        self.config = updated

    def clear_guild(self, guild_id):
        for key in list(self.history):
            if key[0] == guild_id:
                del self.history[key]

    async def request(self, method, endpoint, payload=None):
        api_key = os.getenv('GEMINI_API_KEY', '').strip()
        if not api_key:
            raise AIError('Gemini APIキーが未設定です。PCの Discord-GameBot/.env に GEMINI_API_KEY を設定してBotを再起動してください。')
        try:
            async with self.api_slots:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
                    async with session.request(method, API_ROOT + endpoint,
                                               headers={'x-goog-api-key': api_key}, json=payload) as response:
                        if response.status != 200:
                            messages = {
                                400: 'モデル・思考レベルの組み合わせ、または入力内容が無効です。./ai reasoning auto でもう一度試してください。',
                                401: 'Gemini APIキーの認証に失敗しました。PC側の設定を確認してください。',
                                403: 'Gemini APIへのアクセスが拒否されました。キーの権限・利用地域などを確認してください。',
                                404: 'モデルが見つかりません。./ai models で利用可能なIDを確認してください。',
                                429: 'Geminiの利用上限に達しました。しばらく待ってから試してください。日次上限の場合はリセットまで利用できません。',
                            }
                            raise AIError(messages.get(response.status, f'Gemini側でエラーが発生しました（HTTP {response.status}）。後で試してください。'))
                        return await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise AIError('Geminiとの通信に失敗、またはタイムアウトしました。時間をおいて試してください。') from None

    async def models(self):
        names = []
        endpoint = '/models?pageSize=1000'
        from urllib.parse import quote
        for _ in range(10):
            data = await self.request('GET', endpoint)
            names.extend(model['name'].removeprefix('models/') for model in data.get('models', [])
                         if 'generateContent' in model.get('supportedGenerationMethods', []))
            token = data.get('nextPageToken')
            if not token:
                break
            endpoint = '/models?pageSize=1000&pageToken=' + quote(token, safe='')
        return sorted(set(names))

    async def send(self, message, text):
        # Count UTF-16 units, matching Discord's message length limit.
        chunks, current, units = [], '', 0
        for char in text:
            size = 2 if ord(char) > 0xffff else 1
            if units + size > 1900:
                chunks.append(current)
                current, units = '', 0
            current += char
            units += size
        if current:
            chunks.append(current)
        for chunk in chunks:
            await message.channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())

    async def chat(self, message, question):
        question = question.strip()
        if not question:
            await self.send(message, '質問を入力してください。例：`./ai chat こんにちは`')
            return
        if len(question) > 6000:
            raise AIError('質問は6000文字以内にしてください。')
        key = (message.guild.id, message.channel.id)
        if key in self.busy:
            raise AIError('このチャンネルではAIが回答中です。回答が終わってから送ってください。')
        user_key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if now - self.last_used.get(user_key, -10) < 5:
            raise AIError('連続利用は5秒ほど間隔をあけてください。')
        if len(self.busy) >= 3:
            raise AIError('現在AIが混み合っています。回答が終わってから試してください。')
        settings = self.settings(message.guild.id)
        generation = {'maxOutputTokens': 16384, **thinking_config(settings['model'], settings['reasoning'])}
        old, timestamp = self.history.get(key, ([], 0))
        if now - timestamp > 3600:
            old = []
        user_content = {'role': 'user', 'parts': [{'text': question}]}
        payload = {'contents': old + [user_content],
                   'systemInstruction': {'parts': [{'text': settings['prompt']}]},
                   'generationConfig': generation}
        self.busy.add(key)
        self.last_used[user_key] = now
        self.last_used.move_to_end(user_key)
        while len(self.last_used) > 1024:
            self.last_used.popitem(last=False)
        try:
            async with message.channel.typing():
                data = await self.request('POST', f"/models/{settings['model']}:generateContent", payload)
                answer, model_content = response_text(data)
                await self.send(message, answer)
                self.history[key] = ((old + [user_content, model_content])[-12:], time.monotonic())
                self.history.move_to_end(key)
                while len(self.history) > 128:
                    self.history.popitem(last=False)
        finally:
            self.busy.discard(key)

    async def command(self, message, body):
        words = body.strip().split(maxsplit=1)
        action = words[0].lower() if words else ''
        value = words[1].strip() if len(words) > 1 else ''
        if action == 'chat':
            await self.chat(message, value)
        elif action == 'settings':
            s = self.settings(message.guild.id)
            await self.send(message, f"モデル：`{s['model']}`\n思考レベル：`{s['reasoning']}`\nプロンプト：\n{s['prompt']}\n\n会話履歴：チャンネル共有、最大6往復・1時間。Bot再起動で消去。")
        elif action == 'models':
            models = await self.models()
            await self.send(message, '**利用可能なモデル（無料とは限りません）**\n' + '\n'.join(f'`{model}`' for model in models))
        elif action in ('model', 'prompt', 'reasoning', 'reset'):
            if not message.author.guild_permissions.manage_guild:
                raise AIError('この操作には「サーバー管理」権限が必要です。')
            if any(key[0] == message.guild.id for key in self.busy):
                raise AIError('AIが回答中です。回答が終わってから設定変更・リセットをしてください。')
            if action == 'reset':
                self.history.pop((message.guild.id, message.channel.id), None)
                await self.send(message, 'このチャンネルの会話履歴を削除しました。')
                return
            s = self.settings(message.guild.id)
            if action == 'model':
                model = value.removeprefix('models/')
                if not re.fullmatch(r'gemini-[A-Za-z0-9.\-]{1,100}', model):
                    raise AIError('正しいGeminiモデルIDを指定してください。./ai models で確認できます。')
                s['model'], s['reasoning'] = model, 'auto'
            elif action == 'prompt':
                if not value or len(value) > 8000:
                    raise AIError('プロンプトは1〜8000文字で指定してください。')
                s['prompt'] = DEFAULT_PROMPT if value == 'reset' else value
            else:
                value = value.lower()
                thinking_config(s['model'], value)
                s['reasoning'] = value
            self.save(message.guild.id, s)
            self.clear_guild(message.guild.id)
            await self.send(message, 'AI設定を保存し、このサーバーの会話履歴をリセットしました。' +
                            (' モデル変更に伴い思考レベルをautoに戻しました。' if action == 'model' else ''))
        else:
            await self.send(message, HELP)

    async def on_message(self, message):
        if message.author.bot or message.webhook_id or not message.guild:
            return
        content = message.content.strip()
        prefix = re.match(r'^\./ai(?:\s+|$)', content, re.IGNORECASE)
        mention = None
        if self.bot.user:
            mention = re.search(rf'<@!?{self.bot.user.id}>', content)
        if not prefix and not mention:
            return
        try:
            if prefix:
                await self.command(message, content[prefix.end():])
            else:
                await self.chat(message, re.sub(rf'<@!?{self.bot.user.id}>', '', content).strip())
        except AIError as exc:
            await self.send(message, str(exc))
        except discord.HTTPException:
            log.warning('Could not send AI response in channel %s', message.channel.id)
        except Exception:
            # Avoid logging raw API exceptions, which may include credentials or user text.
            log.error('Unexpected AI chat failure in channel %s', message.channel.id)
            await self.send(message, 'AI機能でエラーが発生しました。設定ファイルとBotの動作状況を確認してください。')


def register_gemini(bot):
    service = GeminiChat(bot)
    bot.add_listener(service.on_message, 'on_message')
    return service
