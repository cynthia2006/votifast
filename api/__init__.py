import asyncio
import time
import os
from functools import cached_property

import httpx

from typing import Dict, Collection
from http.cookiejar import MozillaCookieJar
from datetime import datetime

from pywidevine import Cdm, PSSH, Key

from .totp import TOTP
from .models import (
    Track,
    TrackSource,
    TrackSourceFormat,
    CoverArt,
    Album,
    AlbumTrack
)

class SpotifyApi:
    CLIENT_VERSION = '1.2.83.224.g3acda086'
    METADATA_API_URL = 'https://api-partner.spotify.com/pathfinder/v2/query'
    TRACK_PLAYBACK_API_URL = 'https://gae2-spclient.spotify.com/track-playback/v1/media/spotify:track:{id}'
    WIDEVINE_LICENSE_API_URL = 'https://gae2-spclient.spotify.com/widevine-license/v1/audio/license'
    SEEK_TABLE_API_URL = 'https://seektables.scdn.co/seektable/{file_id}.json'
    STREAM_URLS_API_URL = 'https://gae2-spclient.spotify.com/storage-resolve/v2/files/audio/interactive/10/{file_id}?version=10000000&product=9&platform=39&alt=json'
    SERVER_TIME_URL = 'https://open.spotify.com/api/server-time'
    SESSION_TOKEN_URL = 'https://open.spotify.com/api/token'
    CLIENT_TOKEN_URL = 'https://clienttoken.spotify.com/v1/clienttoken'

    def __init__(
        self,
        client: httpx.AsyncClient,
        sp_dc: str,
        cdm: Cdm,
        secrets: Dict[int, Collection[int]],
    ) -> None:
        self.user_profile = None
        self.client = client
        self.cdm = cdm
        self.client.cookies.set('sp_dc', sp_dc, domain=".spotify.com")

        version, ciphertext = max(secrets.items(), key=lambda item: int(item[0]))
        self.totp = TOTP(version=version, ciphertext=ciphertext)

    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"

    async def initialize(self):
        self.client.headers.update({
            'Accept': '*/*',
            'Origin': "https://open.spotify.com/",
            'Referer': "https://open.spotify.com/",
            'User-Agent': self.USER_AGENT,
            'Spotify-App-Version': self.CLIENT_VERSION,
            'App-Platform': 'WebPlayer',
        })

        await self._setup_authorization()

        response = await self.client.post(
            self.METADATA_API_URL,
            json={
                'variables': {},
                'operationName': 'accountAttributes',
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": "24aaa3057b69fa91492de26841ad199bd0b330ca95817b7a4d6715150de01827"
                    }
                }
            },
        )
        response.raise_for_status()
        self.user_profile = response.json()

    @cached_property
    def is_premium(self):
        return self.user_profile['data']['me']['account']['product'] == 'PREMIUM'

    @classmethod
    def with_cookies(
        cls,
        session: httpx.AsyncClient,
        path: str | bytes | os.PathLike,
        cdm: Cdm,
        secrets: Dict[int, Collection[int]]
    ) -> SpotifyApi:
        cookies = MozillaCookieJar(path)
        cookies.load(ignore_discard=True, ignore_expires=True)

        def parse_cookie(name):
            return next(
                (
                    cookie.value
                    for cookie in cookies
                    if cookie.name == name and cookie.domain == ".spotify.com"
                ),
                None,
            )

        sp_dc = parse_cookie('sp_dc')
        if sp_dc is None:
            raise ValueError(
                "'sp_dc' cookie not found in cookies. "
                "Make sure you have exported the cookies from the Spotify homepage and are logged in."
            )
        return cls(session, sp_dc, cdm, secrets)

    async def _setup_authorization(self) -> None:
        if 'Authorization' in self.client.headers:
            del self.client.headers['Authorization']

        if 'Client-Token' in self.client.headers:
            del self.client.headers['Client-Token']

        response = await self.client.get(self.SERVER_TIME_URL)
        response.raise_for_status()
        server_time = 1000 * response.json()['serverTime']

        totp = self.totp.generate(timestamp=server_time)
        response = await self.client.get(
            self.SESSION_TOKEN_URL,
            params={
                'reason': 'init',
                'productType': 'web-player',
                'totp': totp,
                'totpServer': totp,
                'totpVer': str(self.totp.version),
            },
        )
        response.raise_for_status()
        authorization_info = response.json()
        if not authorization_info.get('accessToken'):
            raise ValueError("Failed to retrieve access token.")

        response = await self.client.post(self.CLIENT_TOKEN_URL,
            json={
                'client_data': {
                    'client_version': self.CLIENT_VERSION,
                    'client_id': authorization_info['clientId'],
                    'js_sdk_data': {}
                }
            },
            headers={'Accept': 'application/json'})
        response.raise_for_status()

        client_token = response.json()
        if not client_token.get('granted_token'):
            raise ValueError("Failed to retrieve granted token.")

        self.client.headers.update({
            'Authorization': f"Bearer {authorization_info['accessToken']}",
            'Client-Token': client_token['granted_token']['token']
        })
        self.session_auth_expire_time = authorization_info['accessTokenExpirationTimestampMs'] / 1000

    async def _refresh_session_auth(self) -> None:
        timestamp_session_expire = int(self.session_auth_expire_time)
        timestamp_now = time.time()
        if timestamp_now < timestamp_session_expire:
            return
        await self._setup_authorization()

    async def get_widevine_key(self, file_id: str) -> Key:
        """Fetch the AES-128 key for decrypting (using Widevine)."""
        await self._refresh_session_auth()

        response = await self.client.get(self.SEEK_TABLE_API_URL.format(file_id=file_id))
        response.raise_for_status()
        pssh = PSSH(response.json()['pssh'])

        session_id = self.cdm.open()
        challenge = self.cdm.get_license_challenge(session_id, pssh)

        await self._refresh_session_auth()

        response = await self.client.post(self.WIDEVINE_LICENSE_API_URL, content=challenge)
        response.raise_for_status()

        self.cdm.parse_license(session_id, license_message=response.content)
        return next(filter(lambda key: key.type == 'CONTENT', self.cdm.get_keys(session_id)), None)

    async def _get_stream_urls(self, file_id: str) -> str:
        await self._refresh_session_auth()

        response = await self.client.get(self.STREAM_URLS_API_URL.format(file_id=file_id))
        response.raise_for_status()
        return response.json()['cdnurl']

    async def get_track(self, media_id: str) -> Track | None:
        """Fetch track metadata, sources, but without the keys.

        The license server won't be queried for premium streams if the account is not premium.

        This operation is relatively expensive, thus it's not called by the `get_album()` or `get_playlist()`
        methods for each track; they just return track IDs for future invocations of this method.
        """
        await self._refresh_session_auth()

        response = await self.client.get(self.TRACK_PLAYBACK_API_URL.format(id=media_id),
                                         params={'manifestFileFormat': 'file_ids_mp4'})
        response.raise_for_status()

        playback_info = response.json()
        if not playback_info['media']:
            return None
        playback_info = playback_info['media'][f'spotify:track:{media_id}']['item']

        async def parse_source(source):
            source_format = TrackSourceFormat(int(source['format']))

            stream_urls = [httpx.URL(url)
                           for url in await self._get_stream_urls(source['file_id'])]

            return TrackSource(file_id=source['file_id'],
                               format=source_format,
                               bitrate=source['bitrate'],
                               cdns=[httpx.URL(url) for url in stream_urls])

        sources = [await parse_source(source) for source in playback_info['manifest']['file_ids_mp4']]

        metadata = playback_info['metadata']
        return Track(name=metadata['name'],
                     artists=[author['name'] for author in metadata['authors']],
                     album=metadata['group_name'],
                     duration=metadata['duration'] / 1000,
                     covers=[CoverArt(httpx.URL(image['url']), image['width'], image['height'])
                             for image in metadata['images']],
                     sources=sources)

    async def get_album(self, album_id: str) -> Album:
        """Fetch album metadata and track listings.

        Track sources and AES-128 keys are not loaded; must be loaded manually with `get_track()`.
        """
        await self._refresh_session_auth()

        response = await self.client.post(self.METADATA_API_URL, json={
            'variables': {
                'uri': f'spotify:album:{album_id}',
                'offset': 0,
                'locale': '',
                'limit': 5000
            },
            'operationName': 'getAlbum',
            'extensions': {
                'persistedQuery': {
                    'version': 1,
                    'sha256Hash': 'b9bfabef66ed756e5e13f68a942deb60bd4125ec1f1be8cc42769dc0259b4b10'
                }
            }
        })
        response.raise_for_status()

        album = response.json()['data']['albumUnion']

        def parse_tracks(track):
            return AlbumTrack(name=track['name'],
                              artists=[item['profile']['name'] for item in track['artists']['items']],
                              album=album['name'],
                              duration=track['duration']['totalMilliseconds'] / 1000,
                              number=track['trackNumber'],
                              disc=track['discNumber'],
                              playable=track['playability']['playable'],
                              track_id=track['uri'].split(':', 2)[2])

        tracks = [parse_tracks(item['track']) for item in album['tracksV2']['items']]

        return Album(name=album['name'],
                     artists=[item['profile']['name'] for item in album['artists']['items']],
                     date=datetime.fromisoformat(album['date']['isoString']),
                     covers=[CoverArt(httpx.URL(image['url']), image['width'], image['height'])
                             for image in album['coverArt']['sources']],
                     tracks=tracks,
                     label=album['label'],
                     discs=album['discs']['totalCount'])

    async def get_playlist(self, playlist_id: str) -> Collection[AlbumTrack]:
        """Fetch playlist tracks, without associated playlist metadata.

        Track sources and AES-128 keys are not loaded; must be loaded manually with `get_track()`.
        """
        await self._refresh_session_auth()

        response = await self.client.post(self.METADATA_API_URL, json={
            'variables': {
                'uri': f'spotify:playlist:{playlist_id}',
                'offset': 0,
                'limit': 5000,
            },
            'operationName': 'fetchPlaylistContents',
            'extensions': {
                'persistedQuery': {
                    'version': 1,
                    'sha256Hash': '7982b11e21535cd2594badc40030b745671b61a1fa66766e569d45e6364f3422'
                }
            }
        })
        response.raise_for_status()

        playlist = response.json()['data']['playlistV2']

        def parse_tracks(track):
            return AlbumTrack(name=track['name'],
                              artists=[item['profile']['name'] for item in track['artists']['items']],
                              album=track['albumOfTrack']['name'],
                              duration=track['trackDuration']['totalMilliseconds'] / 1000,
                              number=track['trackNumber'],
                              disc=track['discNumber'],
                              playable=track['playability']['playable'],
                              track_id=track['uri'].split(':', 2)[2])

        return [parse_tracks(item['itemV2']['data']) for item in playlist['content']['items']]

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass
