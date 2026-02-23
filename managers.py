import os

import httpx
import aiofiles
import asyncio

from dataclasses import dataclass, field
from pathlib import Path
from asyncio import Future, Queue, QueueShutDown

from typing import Callable, Awaitable

from api import SpotifyApi


@dataclass
class DownloadJob:
    url: httpx.URL
    output: Path
    progress: Callable[[int], Awaitable[None]] | None = None
    done: Future[int] = field(init=False)

    def __post_init__(self):
        self.done = Future()

@dataclass
class DecryptionJob:
    file_id: str
    encrypted: Path
    decrypted: Path
    done: Future[int] = field(init=False)

    def __post_init__(self):
        self.done = Future()

class Downloader:
    queue: Queue[DownloadJob]

    def __init__(self, client: httpx.AsyncClient, *, n_workers: int = 10):
        self.client = client
        self.queue = Queue()
        self.tasks = [
            asyncio.create_task(self._downloader_loop())
            for _ in range(n_workers)
        ]
        self.n_workers = n_workers

    @staticmethod
    async def download_file(client: httpx.AsyncClient, job: DownloadJob):
        async with (
            aiofiles.open(job.output, 'wb') as file,
            client.stream('GET', job.url) as r
        ):
            total = 0

            async for chunk in r.aiter_bytes():
                n = await file.write(chunk)
                total += n
                if job.progress:
                    await job.progress(n)

            job.done.set_result(total)

    async def _downloader_loop(self):
        while True:
            try:
                job = await self.queue.get()
            except QueueShutDown:
                break

            await self.download_file(self.client, job)

            self.queue.task_done()

    def enqueue(self, job):
        self.queue.put_nowait(job)

    def shutdown(self):
        self.queue.shutdown()

    async def join(self):
        await self.queue.join()
        return await asyncio.gather(*self.tasks, return_exceptions=True)

class Decryptor:
    queue: Queue[DecryptionJob]

    def __init__(self, sp: SpotifyApi):
        self.sp = sp
        self.queue = Queue()
        self.task = asyncio.create_task(self._decryptor_loop())

    @staticmethod
    async def decrypt_file(sp: SpotifyApi, job: DecryptionJob):
        key = await sp.get_widevine_key(job.file_id)

        # FIXME This should report errors if any, not just stay silent.
        decrypt_proc = await asyncio.create_subprocess_exec(
            'ffmpeg',
            '-y',
            '-loglevel', 'quiet',
            '-decryption_key', key.key.hex(),
            '-i', os.fsdecode(job.encrypted),
            '-c', 'copy',
            os.fsdecode(job.decrypted)
        )
        await decrypt_proc.communicate(None)
        job.encrypted.unlink()

    async def _decryptor_loop(self):
        while True:
            try:
                job = await self.queue.get()
            except QueueShutDown:
                break

            await self.decrypt_file(self.sp, job)

            self.queue.task_done()

    def enqueue(self, job):
        self.queue.put_nowait(job)

    def shutdown(self):
        self.queue.shutdown()

    async def join(self):
        await self.queue.join()
        return await self.task