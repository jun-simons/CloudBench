"""Uploaded files (e.g. .wav inputs) stored on local disk, metadata in the database.

Agents never get file-store credentials. When a run needs a file, the gateway
puts a short-lived signed URL in ``run.start`` and the agent downloads it from
``/internal/files/{id}``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, BinaryIO

from fastapi import APIRouter, Depends, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from cloudbench.gateway.auth import CurrentPrincipal
from cloudbench.gateway.db import File, Principal
from cloudbench.gateway.errors import ApiError, not_found
from cloudbench.gateway.services import Services, get_services
from cloudbench.gateway.util import new_id, utcnow

if TYPE_CHECKING:
    from cloudbench.gateway.config import GatewaySettings

WAV_MEDIA_TYPE = "audio/wav"
_WAV_ALIASES = {"audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"}
_CHUNK = 1024 * 1024


class FileOut(BaseModel):
    id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


def file_out(f: File) -> FileOut:
    return FileOut(
        id=f.id,
        filename=f.filename,
        media_type=f.media_type,
        size_bytes=f.size_bytes,
        sha256=f.sha256,
        created_at=f.created_at,
    )


@dataclass(frozen=True)
class _Staged:
    path: Path
    size: int
    sha256: str
    head: bytes


def _sniff_media_type(head: bytes, declared: str | None) -> str:
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return WAV_MEDIA_TYPE
    if declared in _WAV_ALIASES:
        # Claims to be WAV but is not; refuse rather than hand garbage to hardware.
        raise ApiError(422, "invalid_file", "File is not a valid RIFF/WAVE file")
    return declared or "application/octet-stream"


def _safe_filename(name: str | None) -> str:
    base = os.path.basename(name or "upload")
    return "".join(c for c in base if c.isprintable() and c not in '/\\"')[:120] or "upload"


class FileStore:
    def __init__(self, settings: GatewaySettings) -> None:
        self._settings = settings
        self._dir = settings.files_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._services: Services | None = None

    def bind(self, services: Services) -> None:
        self._services = services

    @property
    def s(self) -> Services:
        assert self._services is not None
        return self._services

    def path(self, file_id: str) -> Path:
        return self._dir / file_id

    async def save_upload(self, owner: Principal, upload: UploadFile) -> File:
        staged = await asyncio.to_thread(self._stage, upload.file, self._settings.max_upload_bytes)
        try:
            media_type = _sniff_media_type(staged.head, upload.content_type)
            return await self._commit(staged, owner.id, _safe_filename(upload.filename), media_type)
        finally:
            await asyncio.to_thread(staged.path.unlink, missing_ok=True)

    async def save_bytes(
        self, owner_id: str | None, filename: str, data: bytes, media_type: str | None = None
    ) -> File:
        staged = await asyncio.to_thread(self._stage, io.BytesIO(data), None)
        try:
            return await self._commit(
                staged,
                owner_id,
                _safe_filename(filename),
                _sniff_media_type(staged.head, media_type),
            )
        finally:
            await asyncio.to_thread(staged.path.unlink, missing_ok=True)

    def _stage(self, source: BinaryIO, limit: int | None) -> _Staged:
        """Copy ``source`` to a temp file in the store, hashing as we go (runs in a thread)."""
        digest = hashlib.sha256()
        size = 0
        head = b""
        fd, tmp_name = tempfile.mkstemp(dir=self._dir, prefix=".upload-")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := source.read(_CHUNK):
                    size += len(chunk)
                    if limit is not None and size > limit:
                        message = f"Files are limited to {limit} bytes"
                        raise ApiError(413, "file_too_large", message)
                    if len(head) < 12:
                        head += chunk[: 12 - len(head)]
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise ApiError(422, "invalid_file", "File is empty")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return _Staged(path=tmp, size=size, sha256=digest.hexdigest(), head=head)

    async def _commit(
        self, staged: _Staged, owner_id: str | None, filename: str, media_type: str
    ) -> File:
        row = File(
            id=new_id("file"),
            owner_id=owner_id,
            filename=filename,
            media_type=media_type,
            size_bytes=staged.size,
            sha256=staged.sha256,
            created_at=utcnow(),
        )
        await asyncio.to_thread(staged.path.replace, self.path(row.id))
        async with self.s.db.session() as s:
            s.add(row)
            await s.commit()
        return row

    # ------------------------------------------------------------ signed URLs

    def _signature(self, file_id: str, expires: int) -> str:
        msg = f"{file_id}:{expires}".encode()
        return hmac.new(self._settings.signing_secret.encode(), msg, hashlib.sha256).hexdigest()

    def signed_path(self, file_id: str) -> str:
        expires = int(time.time() + self._settings.file_url_ttl_s)
        return (
            f"/internal/files/{file_id}?expires={expires}&sig={self._signature(file_id, expires)}"
        )

    def verify(self, file_id: str, expires: int, sig: str) -> bool:
        if expires < time.time():
            return False
        return hmac.compare_digest(sig, self._signature(file_id, expires))


def can_read(principal: Principal, f: File) -> bool:
    return f.owner_id is None or f.owner_id == principal.id


router = APIRouter()


@router.post("/v1/files", status_code=201, tags=["files"])
async def upload_file(
    file: UploadFile,
    principal: CurrentPrincipal,
    services: Annotated[Services, Depends(get_services)],
) -> FileOut:
    """Upload a file (multipart field ``file``). WAV files are verified by their header."""
    return file_out(await services.files.save_upload(principal, file))


@router.get("/v1/files/{file_id}", tags=["files"])
async def get_file(
    file_id: str, principal: CurrentPrincipal, services: Annotated[Services, Depends(get_services)]
) -> FileOut:
    async with services.db.session() as s:
        f = await s.get(File, file_id)
    if f is None or not can_read(principal, f):
        raise not_found("file", file_id)
    return file_out(f)


@router.get("/v1/files/{file_id}/content", tags=["files"])
async def get_file_content(
    file_id: str, principal: CurrentPrincipal, services: Annotated[Services, Depends(get_services)]
) -> FileResponse:
    async with services.db.session() as s:
        f = await s.get(File, file_id)
    if f is None or not can_read(principal, f):
        raise not_found("file", file_id)
    return FileResponse(services.files.path(f.id), media_type=f.media_type, filename=f.filename)


@router.get("/v1/me/files", tags=["library"])
async def list_my_files(
    principal: CurrentPrincipal, services: Annotated[Services, Depends(get_services)]
) -> list[FileOut]:
    async with services.db.session() as s:
        result = await s.execute(
            select(File).where(File.owner_id == principal.id).order_by(File.created_at.desc())
        )
        return [file_out(f) for f in result.scalars()]


@router.get("/internal/files/{file_id}", include_in_schema=False)
async def agent_download(
    file_id: str,
    expires: Annotated[int, Query()],
    sig: Annotated[str, Query()],
    services: Annotated[Services, Depends(get_services)],
) -> FileResponse:
    if not services.files.verify(file_id, expires, sig):
        raise ApiError(403, "invalid_signature", "Download link is invalid or expired")
    async with services.db.session() as s:
        f = await s.get(File, file_id)
    if f is None:
        raise not_found("file", file_id)
    return FileResponse(services.files.path(f.id), media_type=f.media_type)
