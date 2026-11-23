"""Lightweight YouTube validation shared by Bot and isolated workers."""
import re
from urllib.parse import parse_qs, urlsplit

from sound_common import MAX_BYTES, SoundError

MAX_DURATION = 30 * 60
MAX_QUEUE = 50
SEARCH_LIMIT = 7


def normalize_url(value):
    value = value.strip()
    if value.startswith('<') and value.endswith('>'):
        value = value[1:-1]
    if len(value) > 2048:
        raise SoundError('YouTube動画のURLを指定してください。')
    try:
        url = urlsplit(value)
        host = (url.hostname or '').lower()
        if host.startswith('music.amazon.') or host in ('amzn.to', 'a.co'):
            raise SoundError('Amazon Musicの直接再生には現在対応していません。/search でYouTubeを検索するか、/sound play で音声ファイルを添付してください。')
        if url.scheme != 'https' or url.username or url.password or url.port:
            raise ValueError()
        parts = url.path.strip('/').split('/')
        if host in ('youtu.be', 'www.youtu.be') and len(parts) == 1:
            video_id = parts[0]
        elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
            if url.path == '/watch':
                ids = parse_qs(url.query).get('v', [])
                if len(ids) != 1:
                    raise ValueError()
                video_id = ids[0]
            elif len(parts) == 2 and parts[0] in ('shorts', 'embed', 'live'):
                video_id = parts[1]
            else:
                raise ValueError()
        else:
            raise ValueError()
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise ValueError()
        # Never follow arbitrary input hosts, playlist links or user query options.
        return 'https://www.youtube.com/watch?v=' + video_id
    except ValueError:
        raise SoundError('https://www.youtube.com/watch?v=... または https://youtu.be/... の動画URLを指定してください。') from None


def normalize_playlist(value):
    value = value.strip().removeprefix('<').removesuffix('>')
    try:
        if len(value) > 2048:
            raise ValueError()
        url = urlsplit(value)
        ids = parse_qs(url.query).get('list', [])
        if not ids:
            return None
        if (url.scheme != 'https' or url.username is not None or url.password is not None or url.port
                or (url.hostname or '').lower() not in ('youtube.com', 'www.youtube.com', 'm.youtube.com',
                    'music.youtube.com', 'youtu.be', 'www.youtu.be')
                or len(ids) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{10,100}', ids[0])):
            raise ValueError()
        return 'https://www.youtube.com/playlist?list=' + ids[0]
    except ValueError:
        raise SoundError('公開されているYouTubeプレイリストのURLを指定してください。') from None


def normalize_search(query):
    query = ' '.join(query.split())
    if not query or len(query) > 200 or any(ord(char) < 32 for char in query):
        raise SoundError('検索ワードを1〜200文字で入力してください。空白を含むキーワードも使えます。')
    return query
