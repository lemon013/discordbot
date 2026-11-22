"""Isolated, terminable YouTube extraction. Stdout contains only a safe result."""
import json
import math
from pathlib import Path
import sys

from youtube_play import normalize_url, MAX_BYTES, MAX_DURATION


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


if __name__ == '__main__':
    result = download(sys.argv[1], sys.argv[2]) if len(sys.argv) == 3 else {'ok': False, 'reason': 'unavailable'}
    print(json.dumps(result, ensure_ascii=True))
