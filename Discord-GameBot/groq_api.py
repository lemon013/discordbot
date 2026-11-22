"""Groq transport adapter; keep credentials and reasoning out of replies."""
import asyncio
import copy
import os
import re

import aiohttp

API_ROOT = 'https://api.groq.com/openai/v1'
MODELS = ('openai/gpt-oss-20b', 'openai/gpt-oss-120b')
DEFAULT_MODEL = MODELS[0]


class GroqError(Exception):
    """Only fixed, user-safe messages may be raised by this module."""


def completion_payload(model, payload):
    if model not in MODELS:
        raise GroqError('対応しているGroqモデルは openai/gpt-oss-20b / openai/gpt-oss-120b です。')
    system = payload.get('systemInstruction') or payload.get('system_instruction') or {}
    messages = [{'role': 'system', 'content': '\n'.join(
        part['text'] for part in system.get('parts', []) if isinstance(part.get('text'), str))}]
    for content in payload.get('contents', []):
        role = {'user': 'user', 'model': 'assistant'}.get(content.get('role'))
        if role is None:
            raise GroqError('AIへの会話形式を確認できませんでした。')
        text = '\n'.join(part['text'] for part in content.get('parts', [])
                         if isinstance(part.get('text'), str) and not part.get('thought'))
        messages.append({'role': role, 'content': text})
    generation = payload.get('generationConfig') or {}
    result = {'model': model, 'messages': messages, 'stream': False,
              'include_reasoning': False, 'reasoning_effort': generation.get('reasoning_effort', 'low'),
              'max_completion_tokens': min(generation.get('maxOutputTokens', 4096), 4096)}
    if generation.get('responseMimeType') == 'application/json':
        schema = copy.deepcopy(generation.get('responseJsonSchema'))
        if schema:
            schema['additionalProperties'] = False
            result['response_format'] = {'type': 'json_schema', 'json_schema': {
                'name': 'aki_turn', 'strict': True, 'schema': schema}}
        else:
            result['response_format'] = {'type': 'json_object'}
    return result


def normalized_response(data):
    try:
        choice = data['choices'][0]
        text = choice['message']['content']
        if not isinstance(text, str) or not text.strip():
            raise ValueError()
        finish = 'MAX_TOKENS' if choice.get('finish_reason') == 'length' else 'STOP'
        if choice.get('finish_reason') not in ('stop', 'length'):
            raise ValueError()
        # Never retain reasoning, tool calls or arbitrary provider fields.
        return {'candidates': [{'finishReason': finish,
            'content': {'role': 'model', 'parts': [{'text': text.strip()}]}}]}
    except (KeyError, IndexError, TypeError, ValueError):
        raise GroqError('Groqから回答を取得できませんでした。時間をおいて再試行してください。') from None


async def request(method, endpoint, payload, api_slots):
    key = os.getenv('GROQ_API_KEY', '').strip()
    if not key:
        raise GroqError('Groq APIキーが未設定です。MWSの環境変数またはPCの.envに GROQ_API_KEY を設定して再起動してください。')
    match = re.fullmatch(r'/models/(openai/gpt-oss-(?:20b|120b)):generateContent', endpoint)
    models = method == 'GET' and endpoint.startswith('/models?')
    if method == 'POST' and match:
        path, body = '/chat/completions', completion_payload(match[1], payload)
    elif models:
        path, body = '/models', None
    else:
        raise GroqError('Groqへのリクエスト形式に対応していません。')
    try:
        async with asyncio.timeout(35):
            async with api_slots:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                    async with session.request(method, API_ROOT + path, json=body, allow_redirects=False,
                            headers={'Authorization': f'Bearer {key}'}) as response:
                        if response.status != 200:
                            errors = {
                                400: 'Groqのモデル・思考設定・入力形式を確認してください。GPT-OSSの思考設定は auto / low / medium / high です。',
                                401: 'Groq APIキーの認証に失敗しました。GROQ_API_KEY を確認してください。',
                                403: 'Groqへのアクセスが拒否されました。キーの権限・利用地域を確認してください。',
                                404: 'Groqのモデルが見つかりません。/ai models で確認してください。',
                                429: 'Groqの利用上限に達しました。時間をおいて再試行してください。'}
                            raise GroqError(errors.get(response.status, f'Groqでエラーが発生しました（HTTP {response.status}）。時間をおいて再試行してください。'))
                        data = await response.json()
        if models:
            return {'models': [{'name': item['id'], 'supportedGenerationMethods': ['generateContent']}
                               for item in data['data'] if item.get('id') in MODELS]}
        return normalized_response(data)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError, AttributeError):
        raise GroqError('Groqとの通信に失敗、またはタイムアウトしました。時間をおいて再試行してください。') from None
