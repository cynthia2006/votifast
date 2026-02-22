import httpx

from dataclasses import dataclass, field, InitVar
from datetime import datetime
from functools import cached_property
from enum import Enum
from typing import Collection

@dataclass(frozen=True)
class TrackStub:
    name: str
    artists: Collection[str]
    album: str
    duration: float

    @cached_property
    def artist_line(self):
        match len(self.artists):
            case 1:
                return self.artists[0]
            case 2:
                return f'{self.artists[0]} & {self.artists[1]}'
            case _:
                return ', '.join(self.artists[:-1]) + '&' + self.artists[-1]

@dataclass(frozen=True)
class Track(TrackStub):
    covers: Collection[CoverArt]
    sources: Collection[TrackSource]

    @cached_property
    def hq_cover(self) -> CoverArt:
        return max(self.covers)

    @cached_property
    def hq_free_source(self) -> TrackSource:
        return max(filter(lambda src: src.format == TrackSourceFormat.FREE,
                          self.sources),
                   key=lambda src: src.bitrate)

@dataclass(frozen=True)
class TrackSource:
    file_id: str
    format: TrackSourceFormat
    bitrate: int

    # There's a support tier for HTTP version.
    #
    # HTTP/1.1: https://audio-ak.spotifycdn.com and
    #           https://audio-fa.scdn.co
    # HTTP/2: https://audio-fa-tls13.spotifycdn.com and
    #         https://audio-fa-tls130.spotifycdn.com
    # HTTP/3: https://audio-fa-quic.spotifycdn.com and
    #         https://audio-fa-quic0.spotifycdn.com
    cdns: Collection[httpx.URL]

class TrackSourceFormat(Enum):
    FREE = 10
    PREMIUM = 11

@dataclass(frozen=True)
class Album:
    name: str
    artists: Collection[str]
    date: datetime
    covers: Collection[CoverArt]
    items: Collection[AlbumTrack]
    label: str
    discs: int

    @cached_property
    def hq_cover(self):
        return max(self.covers)

@dataclass(frozen=True)
class AlbumTrack(TrackStub):
    number: int
    disc: int
    playable: bool
    track_id: str

@dataclass(frozen=True, order=True)
class CoverArt:
    url: httpx.URL = field(compare=False)
    width: int
    height: int
