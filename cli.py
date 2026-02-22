import asyncio
import re
import httpx
import click
import aiofiles

from typing import Tuple
from pathlib import PurePath
from pywidevine import RemoteCdm

from api import SpotifyApi

SECRETS_URL = 'https://code.thetadev.de/ThetaDev/spotify-secrets/raw/branch/main/secrets/secretDict.json'

def extract_type_and_id(url: str) -> Tuple[str, str]:
    match = re.match(r'https://open.spotify.com/(?P<kind>album|track|playlist)/(?P<id>\w+)', url, flags=re.ASCII)
    return match.group('kind'), match.group('id')

async def download_file(session: httpx.AsyncClient, url: httpx.URL) -> str:
    filename = PurePath(url.path).name

    async with (aiofiles.open(filename, 'wb', buffering=0) as file,
                session.stream('GET', url) as r):
        async for chunk in r.aiter_bytes():
            await file.write(chunk)

    return filename

async def votifast_async(url: str):
    media_type, media_id = extract_type_and_id(url)

    if media_type != 'track':
        raise NotImplementedError

    session = httpx.AsyncClient(http2=True, timeout=None)
    cdm = RemoteCdm('ANDROID', 22590,3,
                    'https://cdrm-project.com/remotecdm/widevine',
                    'CDRM','public')
    secrets = (await session.get(SECRETS_URL)).json()

    async with SpotifyApi.with_cookies(session, 'cookies.txt', cdm, secrets) as sp:
        track = await sp.get_track(media_id)
        encrypted_track = await download_file(session, track.hq_free_source.cdns[0])
        decrypted_track = f'{track.artist_line} - {track.name}.m4a'

        key = await sp.get_widevine_key(track.hq_free_source.file_id)
        decrypt_proc = await asyncio.create_subprocess_exec(
            'ffmpeg',
            '-loglevel', 'error',
            '-decryption_key', key.key.hex(),
            '-i', encrypted_track,
            '-c', 'copy',
            decrypted_track
        )
        await decrypt_proc.communicate(None)

@click.command()
@click.argument('url')
def votifast(*args, **kwargs):
    asyncio.run(votifast_async(*args, **kwargs))

if __name__ == '__main__':
    votifast()
