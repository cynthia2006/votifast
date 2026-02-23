import asyncio
import re
import json

import httpx
import click

from typing import Tuple, Collection
from pathlib import Path
from pywidevine import RemoteCdm

from api import SpotifyApi
from managers import Downloader, DownloadJob, Decryptor, DecryptionJob


SECRETS_URL = 'https://code.thetadev.de/ThetaDev/spotify-secrets/raw/branch/main/secrets/secretDict.json'


def extract_type_and_id(url: str) -> Tuple[str, str]:
    match = re.match(r'https://open.spotify.com/(?P<kind>album|track|playlist)/(?P<id>\w+)', url, flags=re.ASCII)
    return match.group('kind'), match.group('id')

def select_best_cdn(cdns: Collection[httpx.URL]) -> httpx.URL:
    slower_cdn = None
    faster_cdn = None

    for cdn in cdns:
        if cdn.netloc in ('https://audio-ak.spotifycdn.com',
                          'https://audio-fa.scdn.co'):
            slower_cdn = cdn
        else:
            faster_cdn = cdn

    return faster_cdn or slower_cdn

async def votifast_async(
    secrets: Path,
    cookies: Path,
    url: str
):
    media_type, media_id = extract_type_and_id(url)
    client = httpx.AsyncClient(http2=True, timeout=None)
    cdm = RemoteCdm('ANDROID', 22590, 3,
                    'https://cdrm-project.com/remotecdm/widevine',
                    'CDRM', 'public')
    sp = SpotifyApi.with_cookies(
        client, cookies, cdm, secrets=json.load(secrets.open()))

    downloader = Downloader(client)
    decryptor = Decryptor(sp)

    async def enqueue_track(media_id):
        track = await sp.get_track(media_id)
        source = track.hq_source(sp.is_premium)

        interim_path = Path(f'{source.file_id}.bin')
        final_path = Path(f'{track.artist_line} - {track.name}.m4a')

        decrypt_job = DecryptionJob(source.file_id, interim_path, final_path)

        if not interim_path.exists() and not final_path.exists():
            print(f"Downloading {track.name}")

            download_job = DownloadJob(select_best_cdn(source.cdns), interim_path)
            download_job.done.add_done_callback(
                lambda _: decryptor.enqueue(decrypt_job)
            )
            downloader.enqueue(download_job)
        elif interim_path.exists() and not final_path.exists():
            print(f"Already downloaded {track.name}, just decrypting")

            decryptor.enqueue(decrypt_job)

    async with sp:
        match media_type:
            case 'track':
                await enqueue_track(media_id)

                downloader.shutdown()
            case 'playlist':
                playlist = await sp.get_playlist(media_id)
                for track in playlist:
                    await enqueue_track(track.track_id)
                downloader.shutdown()

            case 'album':
                album = await sp.get_album(media_id)
                for track in album.tracks:
                    await enqueue_track(track.track_id)
                downloader.shutdown()

    await downloader.join()
    decryptor.shutdown()
    await decryptor.join()

@click.command()
@click.option('--secrets', type=Path, default='secrets.json', help="Spotify secrets for TOTP generation")
@click.option('--cookies', type=Path, default='cookies.txt', help="Spoitfy cookies for authentication")
@click.argument('url')
def votifast(*args, **kwargs):
    asyncio.run(votifast_async(*args, **kwargs), debug=True)

if __name__ == '__main__':
    votifast()
