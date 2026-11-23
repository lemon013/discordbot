"""Isolated, terminable YouTube extraction. Stdout contains only a safe result."""
import json
import math
from itertools import islice
from pathlib import Path
import sys

from youtube_rules import normalize_url, normalize_playlist, normalize_search, MAX_BYTES, MAX_DURATION, MAX_QUEUE, SEARCH_LIMIT


class QuietLogger:
    def debug(self, *args, **kwargs):
        pass
    info = warning = error = debug


def download(url, destination):
    try:
        import yt_dlp
        import deno
        runtime = deno.find_deno_bin()
        if not Path(runtime).is_file():
            raise ImportError()
    except (ImportError, RuntimeError, OSError):
        return {'ok': False, 'reason': 'dependency'}
    reason = None

    def match(info, *, incomplete=False):
        nonlocal reason
        if incomplete:
            return None
        if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming', 'post_live'):
            reason = 'live'
        else:
            duration = info.get('duration')
            if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
                reason = 'unavailable'
            elif duration > MAX_DURATION:
                reason = 'duration'
            elif (info.get('filesize') or 0) > MAX_BYTES:
                reason = 'size'
        return reason

    def progress(status):
        nonlocal reason
        if max(status.get('downloaded_bytes') or 0, status.get('total_bytes') or 0) > MAX_BYTES:
            reason = 'size'
            raise RuntimeError('size')

    options = {
        'format': 'bestaudio[protocol=https]', 'outtmpl': str(destination),
        'noplaylist': True, 'quiet': True, 'no_warnings': True, 'noprogress': True,
        'cachedir': False, 'logger': QuietLogger(), 'match_filter': match,
        'max_filesize': MAX_BYTES, 'progress_hooks': [progress],
        'socket_timeout': 15, 'retries': 1, 'fragment_retries': 1,
        'concurrent_fragment_downloads': 1, 'buffersize': 65536,
        'js_runtimes': {'deno': {'path': runtime}},
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(normalize_url(url), download=True)
        if reason:
            return {'ok': False, 'reason': reason}
        if not info or info.get('extractor_key') != 'Youtube' or not Path(destination).is_file():
            return {'ok': False, 'reason': 'unavailable'}
        if not 0 < Path(destination).stat().st_size <= MAX_BYTES:
            return {'ok': False, 'reason': 'size'}
        return {'ok': True, 'title': str(info.get('title') or 'YouTube音声')[:150]}
    except Exception:
        return {'ok': False, 'reason': reason or 'unavailable'}


def playlist(url):
    try:
        import yt_dlp
        import deno
        runtime = deno.find_deno_bin()
    except (ImportError, RuntimeError, OSError):
        return {'ok': False, 'reason': 'dependency'}
    try:
        canonical = normalize_playlist(url)
        if canonical is None:
            return {'ok': False, 'reason': 'unavailable'}
        with yt_dlp.YoutubeDL({'extract_flat': 'in_playlist', 'noplaylist': False,
                'playlistend': MAX_QUEUE, 'lazy_playlist': True, 'ignoreerrors': True,
                'quiet': True, 'no_warnings': True, 'cachedir': False, 'logger': QuietLogger(),
                'socket_timeout': 15, 'retries': 1, 'js_runtimes': {'deno': {'path': runtime}}}) as downloader:
            info = downloader.extract_info(canonical, download=False)
            if not info or info.get('_type') not in ('playlist', 'multi_video'):
                return {'ok': False, 'reason': 'unavailable'}
            raw = list(islice(info.get('entries') or [], MAX_QUEUE))
            entries = []
            for entry in raw:
                if not entry or entry.get('availability') in ('private', 'premium_only', 'subscriber_only', 'needs_auth'):
                    continue
                try:
                    video = normalize_url('https://www.youtube.com/watch?v=' + str(entry.get('id', '')))
                except Exception:
                    continue
                entries.append({'url': video, 'title': str(entry.get('title') or '')[:150]})
            if not entries:
                return {'ok': False, 'reason': 'unavailable'}
            return {'ok': True, 'entries': entries, 'limited': len(raw) >= MAX_QUEUE}
    except Exception:
        return {'ok': False, 'reason': 'unavailable'}


def search(query):
    try:
        import yt_dlp
    except ImportError:
        return {'ok': False, 'reason': 'dependency'}
    try:
        # Construct a search directive ourselves; input is never an extraction URL.
        query = normalize_search(query)
        with yt_dlp.YoutubeDL({'extract_flat': True, 'skip_download': True,
                'playlistend': SEARCH_LIMIT, 'ignoreerrors': True,
                'quiet': True, 'no_warnings': True, 'cachedir': False, 'logger': QuietLogger(),
                'socket_timeout': 10, 'retries': 0}) as downloader:
            info = downloader.extract_info(f'ytsearch{SEARCH_LIMIT}:' + query, download=False)
            if not info or not isinstance(info, dict) or 'entries' not in info:
                return {'ok': False, 'reason': 'unavailable'}
            entries, seen = [], set()
            for entry in islice(info.get('entries') or [], SEARCH_LIMIT):
                if not entry:
                    continue
                try:
                    url = normalize_url('https://www.youtube.com/watch?v=' + str(entry.get('id', '')))
                except Exception:
                    continue
                if url in seen:
                    continue
                seen.add(url)
                entries.append({'url': url, 'title': str(entry.get('title') or 'YouTube動画')[:150]})
            return {'ok': True, 'entries': entries}
    except Exception:
        return {'ok': False, 'reason': 'unavailable'}


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[3] == 'search':
        result = search(sys.argv[1])
    elif len(sys.argv) == 4 and sys.argv[3] == 'playlist':
        result = playlist(sys.argv[1])
    else:
        result = download(sys.argv[1], sys.argv[2]) if len(sys.argv) == 3 else {'ok': False, 'reason': 'unavailable'}
    print(json.dumps(result, ensure_ascii=True))
