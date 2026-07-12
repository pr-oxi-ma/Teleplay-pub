"""Recycle-bin settings, read-only browsing, restore and permanent-delete endpoints."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import get_current_user
from ..database import get_db
from ..models import File, Folder, User
from ..recycle_bin import (
    get_descendant_folder_ids,
    get_or_create_recycle_settings,
    merge_duplicate_trash_roots,
    permanently_delete_files,
    permanently_delete_folder,
    restore_file,
    restore_folder,
)
from ..schemas import (
    FileResponse,
    RecycleBinSettingsResponse,
    RecycleBinSettingsUpdate,
    TrashBreadcrumbResponse,
    TrashBrowseResponse,
    TrashBulkRequest,
    TrashFolderResponse,
    TrashListResponse,
)
from ..services import add_urls_to_file, fetch_progress_positions, is_image_file_record
from .streaming import stream_file_response, thumbnail_response

router = APIRouter(prefix="/trash", tags=["Recycle Bin"])


def _trash_file_response(item: File, last_pos: int = 0) -> FileResponse:
    data = add_urls_to_file(item, last_pos)
    has_thumbnail_endpoint = bool(item.thumbnail_file_id) or is_image_file_record(item)
    data.update(
        stream_url=f"/api/trash/files/{item.id}/stream",
        fallback_stream_url=None,
        download_url=f"/api/trash/files/{item.id}/stream?download=1",
        thumbnail_url=(
            f"/api/trash/files/{item.id}/thumbnail"
            if has_thumbnail_endpoint
            else None
        ),
        public_hash=None,
        public_stream_url=None,
    )
    return FileResponse(**data)


async def _trash_folder_item_count(
    db: AsyncSession,
    user_id: int,
    folder_id: int,
    trash_root_id: int,
) -> int:
    raw_ids = await get_descendant_folder_ids(
        db, folder_id, user_id, include_deleted=True
    )
    if not raw_ids:
        return 0

    folder_count = (
        await db.execute(
            select(func.count(Folder.id))
            .where(
                Folder.user_id == user_id,
                Folder.id.in_(raw_ids),
                Folder.id != folder_id,
                Folder.deleted_at.is_not(None),
                Folder.trash_root_id == trash_root_id,
            )
            .execution_options(include_deleted=True)
        )
    ).scalar() or 0
    file_count = (
        await db.execute(
            select(func.count(File.id))
            .where(
                File.user_id == user_id,
                File.folder_id.in_(raw_ids),
                File.deleted_at.is_not(None),
                File.trash_root_id == trash_root_id,
            )
            .execution_options(include_deleted=True)
        )
    ).scalar() or 0
    return int(folder_count + file_count)


async def _trash_folder_response(
    db: AsyncSession,
    user_id: int,
    folder: Folder,
) -> TrashFolderResponse:
    trash_root_id = int(folder.trash_root_id or folder.id)
    return TrashFolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        deleted_at=folder.deleted_at,
        purge_after=folder.purge_after,
        item_count=await _trash_folder_item_count(
            db, user_id, int(folder.id), trash_root_id
        ),
    )


async def _get_trashed_file(
    db: AsyncSession,
    user_id: int,
    file_id: int,
) -> File:
    result = await db.execute(
        select(File)
        .where(
            File.id == file_id,
            File.user_id == user_id,
            File.deleted_at.is_not(None),
        )
        .execution_options(include_deleted=True)
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Trashed file not found")
    return item


async def _get_trashed_folder(
    db: AsyncSession,
    user_id: int,
    folder_id: int,
) -> Folder:
    result = await db.execute(
        select(Folder)
        .where(
            Folder.id == folder_id,
            Folder.user_id == user_id,
            Folder.deleted_at.is_not(None),
            Folder.trash_root_id.is_not(None),
        )
        .execution_options(include_deleted=True)
    )
    folder = result.scalar_one_or_none()
    if folder is None:
        raise HTTPException(status_code=404, detail="Trashed folder not found")
    return folder


async def _trash_breadcrumbs(
    db: AsyncSession,
    user_id: int,
    folder: Folder,
) -> list[TrashBreadcrumbResponse]:
    root_id = int(folder.trash_root_id or folder.id)
    result = await db.execute(
        select(Folder)
        .where(
            Folder.user_id == user_id,
            Folder.deleted_at.is_not(None),
            Folder.trash_root_id == root_id,
        )
        .execution_options(include_deleted=True)
    )
    folders = {int(item.id): item for item in result.scalars().all()}
    chain: list[TrashBreadcrumbResponse] = []
    current_id: int | None = int(folder.id)
    visited: set[int] = set()
    while current_id is not None and current_id not in visited:
        visited.add(current_id)
        current = folders.get(current_id)
        if current is None:
            break
        chain.append(TrashBreadcrumbResponse(id=current.id, name=current.name))
        if current.id == root_id:
            break
        current_id = current.parent_id
    chain.reverse()
    return chain


@router.get("/settings", response_model=RecycleBinSettingsResponse)
async def get_recycle_bin_settings(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    preferences = await get_or_create_recycle_settings(db, current_user.id)
    await db.commit()
    return RecycleBinSettingsResponse(
        enabled=preferences.recycle_bin_enabled,
        retention_days=preferences.recycle_bin_retention_days,
    )


@router.put("/settings", response_model=RecycleBinSettingsResponse)
async def update_recycle_bin_settings(
    payload: RecycleBinSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    preferences = await get_or_create_recycle_settings(db, current_user.id)
    preferences.recycle_bin_enabled = payload.enabled
    preferences.recycle_bin_retention_days = payload.retention_days

    # Retention is a user policy, so existing items follow the newly selected
    # number of days as well. The UI can immediately recalculate days remaining.
    updated_items = 0
    file_result = await db.execute(
        select(File)
        .where(File.user_id == current_user.id, File.deleted_at.is_not(None))
        .execution_options(include_deleted=True)
    )
    for item in file_result.scalars().all():
        item.purge_after = item.deleted_at + timedelta(days=payload.retention_days)
        updated_items += 1

    folder_result = await db.execute(
        select(Folder)
        .where(Folder.user_id == current_user.id, Folder.deleted_at.is_not(None))
        .execution_options(include_deleted=True)
    )
    for item in folder_result.scalars().all():
        item.purge_after = item.deleted_at + timedelta(days=payload.retention_days)
        updated_items += 1

    await db.commit()
    return RecycleBinSettingsResponse(
        enabled=preferences.recycle_bin_enabled,
        retention_days=preferences.recycle_bin_retention_days,
        updated_items=updated_items,
    )


@router.get("", response_model=TrashListResponse)
async def list_trash(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Repair duplicate roots created by the old partial-restore/delete flow.
    if await merge_duplicate_trash_roots(db, current_user.id):
        await db.commit()

    file_result = await db.execute(
        select(File)
        .where(
            File.user_id == current_user.id,
            File.deleted_at.is_not(None),
            File.trash_root_id.is_(None),
        )
        .order_by(File.deleted_at.desc())
        .execution_options(include_deleted=True)
    )
    file_items = file_result.scalars().all()
    progress_positions = await fetch_progress_positions(
        db, current_user.id, [item.id for item in file_items]
    )
    files = [
        _trash_file_response(item, progress_positions.get(item.id, 0))
        for item in file_items
    ]

    folder_result = await db.execute(
        select(Folder)
        .where(
            Folder.user_id == current_user.id,
            Folder.deleted_at.is_not(None),
            Folder.trash_root_id == Folder.id,
        )
        .order_by(Folder.deleted_at.desc())
        .execution_options(include_deleted=True)
    )
    folders = [
        await _trash_folder_response(db, current_user.id, folder)
        for folder in folder_result.scalars().all()
    ]
    return TrashListResponse(files=files, folders=folders, total=len(files) + len(folders))


@router.get("/folders/{folder_id}/children", response_model=TrashBrowseResponse)
async def browse_trashed_folder(
    folder_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current = await _get_trashed_folder(db, current_user.id, folder_id)
    root_id = int(current.trash_root_id or current.id)

    folder_result = await db.execute(
        select(Folder)
        .where(
            Folder.user_id == current_user.id,
            Folder.parent_id == current.id,
            Folder.deleted_at.is_not(None),
            Folder.trash_root_id == root_id,
        )
        .order_by(Folder.name.asc())
        .execution_options(include_deleted=True)
    )
    folders = [
        await _trash_folder_response(db, current_user.id, folder)
        for folder in folder_result.scalars().all()
    ]

    file_result = await db.execute(
        select(File)
        .where(
            File.user_id == current_user.id,
            File.folder_id == current.id,
            File.deleted_at.is_not(None),
            File.trash_root_id == root_id,
        )
        .order_by(File.file_name.asc())
        .execution_options(include_deleted=True)
    )
    file_items = file_result.scalars().all()
    progress_positions = await fetch_progress_positions(
        db, current_user.id, [item.id for item in file_items]
    )
    files = [
        _trash_file_response(item, progress_positions.get(item.id, 0))
        for item in file_items
    ]

    return TrashBrowseResponse(
        current_folder=await _trash_folder_response(db, current_user.id, current),
        breadcrumbs=await _trash_breadcrumbs(db, current_user.id, current),
        files=files,
        folders=folders,
        total=len(files) + len(folders),
    )


@router.get("/files/{file_id}/stream")
async def stream_trashed_file(
    file_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    download: int = Query(0, description="Set to 1 to force download"),
):
    item = await _get_trashed_file(db, current_user.id, file_id)
    return await stream_file_response(item, request, download)


@router.get("/files/{file_id}/thumbnail")
async def get_trashed_file_thumbnail(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = await _get_trashed_file(db, current_user.id, file_id)
    return await thumbnail_response(item)


@router.post("/files/{file_id}/restore")
async def restore_trashed_file(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not await restore_file(db, current_user.id, file_id):
        raise HTTPException(status_code=404, detail="Trashed file not found")
    await db.commit()
    return {"message": "File restored"}


@router.post("/folders/{folder_id}/restore")
async def restore_trashed_folder(
    folder_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not await restore_folder(db, current_user.id, folder_id):
        raise HTTPException(status_code=404, detail="Trashed folder not found")
    await db.commit()
    return {"message": "Folder restored"}


@router.post("/bulk-restore")
async def bulk_restore(
    payload: TrashBulkRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not payload.file_ids and not payload.folder_ids:
        raise HTTPException(status_code=400, detail="Select at least one item")
    restored_files = restored_folders = 0
    for folder_id in dict.fromkeys(payload.folder_ids):
        restored_folders += int(await restore_folder(db, current_user.id, folder_id))
    for file_id in dict.fromkeys(payload.file_ids):
        restored_files += int(await restore_file(db, current_user.id, file_id))
    await db.commit()
    return {
        "message": f"Restored {restored_files + restored_folders} items",
        "files": restored_files,
        "folders": restored_folders,
    }


@router.delete("/files/{file_id}")
async def delete_trashed_file_forever(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    deleted = await permanently_delete_files(db, current_user.id, [file_id], trashed_only=True)
    if not deleted:
        raise HTTPException(status_code=404, detail="Trashed file not found")
    await db.commit()
    return {"message": "File permanently deleted"}


@router.delete("/folders/{folder_id}")
async def delete_trashed_folder_forever(
    folder_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    folders, files = await permanently_delete_folder(
        db, current_user.id, folder_id, trashed_only=True
    )
    if not folders:
        raise HTTPException(status_code=404, detail="Trashed folder not found")
    await db.commit()
    return {"message": "Folder permanently deleted", "folders": folders, "files": files}


@router.post("/bulk-delete")
async def bulk_delete_forever(
    payload: TrashBulkRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not payload.file_ids and not payload.folder_ids:
        raise HTTPException(status_code=400, detail="Select at least one item")
    deleted_files = await permanently_delete_files(
        db, current_user.id, list(dict.fromkeys(payload.file_ids)), trashed_only=True
    )
    deleted_folders = 0
    for folder_id in dict.fromkeys(payload.folder_ids):
        folder_count, nested_files = await permanently_delete_folder(
            db, current_user.id, folder_id, trashed_only=True
        )
        deleted_folders += folder_count
        deleted_files += nested_files
    await db.commit()
    return {
        "message": "Selected items permanently deleted",
        "files": deleted_files,
        "folders": deleted_folders,
    }


@router.delete("")
async def empty_recycle_bin(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    root_result = await db.execute(
        select(Folder.id)
        .where(
            Folder.user_id == current_user.id,
            Folder.deleted_at.is_not(None),
            Folder.trash_root_id == Folder.id,
        )
        .execution_options(include_deleted=True)
    )
    deleted_folders = deleted_files = 0
    for folder_id in root_result.scalars().all():
        folder_count, file_count = await permanently_delete_folder(
            db, current_user.id, int(folder_id), trashed_only=True
        )
        deleted_folders += folder_count
        deleted_files += file_count

    file_result = await db.execute(
        select(File.id)
        .where(
            File.user_id == current_user.id,
            File.deleted_at.is_not(None),
            File.trash_root_id.is_(None),
        )
        .execution_options(include_deleted=True)
    )
    deleted_files += await permanently_delete_files(
        db,
        current_user.id,
        [int(value) for value in file_result.scalars().all()],
        trashed_only=True,
    )
    await db.commit()
    return {
        "message": "Recycle Bin emptied",
        "files": deleted_files,
        "folders": deleted_folders,
    }
