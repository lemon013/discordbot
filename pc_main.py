"""Run only the local Minecraft bridge and outbound score uploader."""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / 'PC-Bridge'
sys.path.insert(0, str(ROOT / 'Discord-GameBot'))


async def upload_loop(process, token, channel, secret):
    import aiohttp
    from snapshot_sync import encode_snapshot, REGISTRY, SNAPSHOTS
    path = LOCAL / '.discord-mcbe' / 'data.json'
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30),
                                     headers={'Authorization': f'Bot {token}'}) as session:
        while process.poll() is None:
            try:
                data = json.loads(path.read_text(encoding='utf-8-sig'))
                if REGISTRY not in data or SNAPSHOTS not in data:
                    logging.info('Waiting for registered Minecraft scoreboards')
                else:
                    form = aiohttp.FormData()
                    form.add_field('payload_json', json.dumps({'content': 'minecraft-status-sync-v1', 'allowed_mentions': {'parse': []}}))
                    form.add_field('files[0]', encode_snapshot(data, secret),
                                   filename='minecraft-status.json', content_type='application/json')
                    async with session.post(f'https://discord.com/api/v10/channels/{channel}/messages', data=form) as response:
                        if response.status not in (200, 201):
                            # Do not print response bodies, tokens or signatures.
                            logging.error('Score upload failed: HTTP %s (check channel permissions)', response.status)
                        else:
                            logging.info('Scores sent to cloud sync channel')
            except FileNotFoundError:
                logging.info('Waiting for first Minecraft score capture')
            except (json.JSONDecodeError, OSError):
                logging.warning('Score file is being updated; retrying')
            except Exception:
                logging.exception('Score upload failed; retrying')
            for _ in range(60):
                if process.poll() is not None:
                    return
                await asyncio.sleep(1)


def main():
    try:
        from dotenv import load_dotenv
        import aiohttp
    except ImportError:
        print('Install dependencies first: py -3 -m pip install -r requirements.txt')
        return 1
    logging.basicConfig(level=logging.INFO, format='[PC] %(levelname)s: %(message)s')
    env_path = LOCAL / '.env'
    if not env_path.exists():
        shutil.copyfile(LOCAL / '.env.example', env_path)
        print('PC-Bridge/.env created. Fill it in before starting again.')
        return 1
    load_dotenv(env_path, override=True)
    token = os.getenv('DISCORD_TOKEN', '')
    channel = os.getenv('MC_SYNC_CHANNEL_ID', '')
    secret = os.getenv('MC_SYNC_SECRET', '')
    if not token or not channel.isdecimal() or len(secret) < 32 or not all(os.getenv(k, '').isdecimal() for k in ('GUILD_ID', 'CHANNEL_ID')):
        print('Fill DISCORD_TOKEN, GUILD_ID, CHANNEL_ID, MC_SYNC_CHANNEL_ID and MC_SYNC_SECRET (32+ characters) in PC-Bridge/.env')
        return 1
    if os.getenv('CHANNEL_ID') == channel:
        print('Use different channels for chat and private score sync.')
        return 1
    # Keep the original PC snapshot; copy once, never overwrite newer PC data.
    source = ROOT / 'Minecraft-Discord-Bridge' / '.discord-mcbe' / 'data.json'
    target = LOCAL / '.discord-mcbe' / 'data.json'
    if source.exists() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    updater = ROOT / 'Minecraft-Discord-Bridge' / 'updater.exe'
    bun = shutil.which('bun')
    if bun:
        runtime = [bun]
    elif os.name == 'nt' and updater.exists():
        runtime = [str(updater)]
        os.environ['BUN_BE_BUN'] = '1'
    else:
        print('Bun is required, or keep the existing Minecraft-Discord-Bridge/updater.exe on Windows.')
        return 1
    subprocess.run(runtime + ['install'], cwd=LOCAL, check=True)
    process = subprocess.Popen(runtime + ['run', 'app.js'], cwd=LOCAL)
    try:
        asyncio.run(upload_loop(process, token, channel, secret))
    except KeyboardInterrupt:
        pass
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return process.returncode or 0


if __name__ == '__main__':
    raise SystemExit(main())
