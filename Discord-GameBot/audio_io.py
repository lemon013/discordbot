"""Bounded audio downloads with cancellation-safe file ownership."""
from pathlib import Path
from audio_cache import disk_call
from sound_common import SoundError


async def stream_audio(response, destination, limit):
    path = Path(destination)
    total = 0
    try:
        with path.open('wb') as output:
            async for chunk in response.content.iter_chunked(65536):
                total += len(chunk)
                if total > limit:
                    raise SoundError('音声サービスの応答が大きすぎます。')
                await disk_call(output.write, chunk)
        if not total:
            raise SoundError('音声サービスの応答が空です。')
        return total
    except BaseException:
        path.unlink(missing_ok=True)
        raise
