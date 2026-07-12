"""
File management API endpoints.
"""
from typing import Optional
from datetime import timedelta
from pydantic import BaseModel, Field, RootModel
from fastapi import APIRouter, Depends, HTTPException, Query
import secrets
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete

from ..database import get_db
from ..models import File, User, WatchProgress, Folder
from ..schemas import FileResponse, FileListResponse, FileUpdate, WatchProgressUpdate
from ..auth import get_current_user
from ..recycle_bin import trash_files
from ..config import get_settings
from ..services import (
    escape_like, 
    sanitize_filename, 
    add_urls_to_file,
    is_image_file_record,
    fetch_progress_positions,
    fetch_recent_files,
    fetch_continue_watching_files,
)
from ..time_utils import utcnow

router = APIRouter(prefix="/files", tags=["Files"])
settings = get_settings()


MAX_BATCH_IDS = 500


class BatchIdsRequest(RootModel[list[int]]):
    root: list[int] = Field(..., min_length=1, max_length=MAX_BATCH_IDS)


class BatchMoveRequest(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=MAX_BATCH_IDS)
    folder_id: Optional[int] = None


@router.get("", response_model=FileListResponse)
async def list_files(
    folder_id: Optional[int] = Query(None, description="Filter by folder ID (null for root)"),
    file_type: Optional[str] = Query(None, description="Filter by file type"),
    search: Optional[str] = Query(None, description="Search by filename"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List user's files with optional filtering."""
    query = select(File).where(File.user_id == current_user.id)

    # Apply filters
    if folder_id is not None:
        query = query.where(File.folder_id == folder_id)
    elif not search and not file_type:
        # If simply browsing (no search/filter), only show files in root (folder_id is NULL)
        query = query.where(File.folder_id.is_(None))
        
    if file_type:
        query = query.where(File.file_type == file_type)
    if search:
        query = query.where(File.file_name.ilike(f"%{escape_like(search)}%", escape="\\"))
    
    # Get total count
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar()
    
    # Apply pagination
    query = query.order_by(File.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)
    
    result = await db.execute(query)
    files = result.scalars().all()
    progress_positions = await fetch_progress_positions(
        db, current_user.id, [file.id for file in files]
    )

    return FileListResponse(
        files=[
            FileResponse(**add_urls_to_file(f, progress_positions.get(f.id, 0)))
            for f in files
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/recent", response_model=FileListResponse)
async def get_recent_files(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get recently added files across all folders."""
    files = await fetch_recent_files(db, current_user.id, limit)
    progress_positions = await fetch_progress_positions(
        db, current_user.id, [file.id for file in files]
    )

    return FileListResponse(
        files=[
            FileResponse(**add_urls_to_file(f, progress_positions.get(f.id, 0)))
            for f in files
        ],
        total=len(files),
        page=1,
        per_page=limit,
    )


@router.get("/continue-watching", response_model=FileListResponse)
async def get_continue_watching(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get files with watch progress."""
    files = await fetch_continue_watching_files(db, current_user.id, limit)
    progress_positions = await fetch_progress_positions(
        db, current_user.id, [file.id for file in files]
    )

    return FileListResponse(
        files=[
            FileResponse(**add_urls_to_file(f, progress_positions.get(f.id, 0)))
            for f in files
        ],
        total=len(files),
        page=1,
        per_page=limit,
    )


@router.get("/storage", response_model=dict)
async def get_storage_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get total storage usage."""
    query = select(func.sum(File.file_size)).where(File.user_id == current_user.id)
    result = await db.execute(query)
    total_size = result.scalar() or 0
    
    return {
        "total_size": total_size,
        "limit": -1  # Unlimited
    }


@router.get("/analytics", response_model=dict)
async def get_storage_analytics(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return storage composition, recent activity, and largest active files."""
    active_summary = await db.execute(
        select(func.count(File.id), func.sum(File.file_size)).where(
            File.user_id == current_user.id
        )
    )
    active_count, active_size = active_summary.one()

    trash_summary = await db.execute(
        select(func.count(File.id), func.sum(File.file_size))
        .where(File.user_id == current_user.id, File.deleted_at.is_not(None))
        .execution_options(include_deleted=True)
    )
    trash_count, trash_size = trash_summary.one()

    type_result = await db.execute(
        select(File.file_type, func.count(File.id), func.sum(File.file_size))
        .where(File.user_id == current_user.id)
        .group_by(File.file_type)
        .order_by(func.sum(File.file_size).desc())
    )
    by_type = [
        {"type": file_type, "count": int(count or 0), "size": int(size or 0)}
        for file_type, count, size in type_result.all()
    ]

    folder_count = (
        await db.execute(
            select(func.count(Folder.id)).where(Folder.user_id == current_user.id)
        )
    ).scalar() or 0

    largest_result = await db.execute(
        select(File)
        .where(File.user_id == current_user.id)
        .order_by(File.file_size.desc())
        .limit(8)
    )
    largest_files = [
        {
            "id": item.id,
            "file_name": item.file_name,
            "file_type": item.file_type,
            "file_size": int(item.file_size),
            "folder_id": item.folder_id,
            "created_at": item.created_at,
            "thumbnail_url": (
                f"/api/stream/{item.id}/thumbnail"
                if item.thumbnail_file_id or is_image_file_record(item)
                else None
            ),
        }
        for item in largest_result.scalars().all()
    ]

    start_day = (utcnow() - timedelta(days=13)).date()
    activity_result = await db.execute(
        select(File.created_at, File.file_size).where(
            File.user_id == current_user.id,
            File.created_at >= utcnow() - timedelta(days=14),
        )
    )
    activity_by_day: dict[str, dict[str, int]] = {}
    for created_at, file_size in activity_result.all():
        key = created_at.date().isoformat()
        bucket = activity_by_day.setdefault(key, {"count": 0, "size": 0})
        bucket["count"] += 1
        bucket["size"] += int(file_size or 0)

    daily_activity = []
    for offset in range(14):
        day = start_day + timedelta(days=offset)
        values = activity_by_day.get(day.isoformat(), {"count": 0, "size": 0})
        daily_activity.append({"date": day.isoformat(), **values})

    return {
        "active": {"count": int(active_count or 0), "size": int(active_size or 0)},
        "trash": {"count": int(trash_count or 0), "size": int(trash_size or 0)},
        "folders": int(folder_count),
        "by_type": by_type,
        "largest_files": largest_files,
        "daily_activity": daily_activity,
        "limit": -1,
    }


@router.delete("/progress")
async def clear_watch_progress(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Clear all saved watch progress for the current user."""
    count_result = await db.execute(
        select(func.count())
        .select_from(WatchProgress)
        .where(WatchProgress.user_id == current_user.id)
    )
    cleared = count_result.scalar() or 0

    await db.execute(
        delete(WatchProgress).where(WatchProgress.user_id == current_user.id)
    )
    await db.commit()

    return {
        "message": "Watch progress cleared",
        "cleared": cleared,
    }


@router.get("/{file_id}", response_model=FileResponse)
async def get_file(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a specific file by ID."""
    result = await db.execute(
        select(File).where(File.id == file_id, File.user_id == current_user.id)
    )
    file = result.scalar_one_or_none()
    
    if not file:
        raise HTTPException(status_code=404, detail="File not found")
    
    progress_positions = await fetch_progress_positions(db, current_user.id, [file.id])
    return FileResponse(**add_urls_to_file(file, progress_positions.get(file.id, 0)))


@router.patch("/{file_id}", response_model=FileResponse)
async def update_file(
    file_id: int,
    update_data: FileUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update file metadata (rename, move to folder)."""
    result = await db.execute(
        select(File).where(File.id == file_id, File.user_id == current_user.id)
    )
    file = result.scalar_one_or_none()
    
    if not file:
        raise HTTPException(status_code=404, detail="File not found")
    
    # Update fields. Pydantic v2 lets us distinguish an omitted field from
    # an explicit null, which is required for moving a file back to root.
    fields_set = update_data.model_fields_set

    if "file_name" in fields_set and update_data.file_name is not None:
        file.file_name = sanitize_filename(update_data.file_name)

    if "folder_id" in fields_set:
        target_folder_id = update_data.folder_id
        if target_folder_id == 0:
            target_folder_id = None

        if target_folder_id is not None:
            folder_check = await db.execute(
                select(Folder.id).where(
                    Folder.id == target_folder_id,
                    Folder.user_id == current_user.id,
                )
            )
            if folder_check.scalar_one_or_none() is None:
                raise HTTPException(status_code=404, detail="Target folder not found")

        file.folder_id = target_folder_id
    
    await db.commit()
    
    # Re-fetch with relationships
    result = await db.execute(
        select(File).where(File.id == file_id, File.user_id == current_user.id)
    )
    file = result.scalar_one()
    
    progress_positions = await fetch_progress_positions(db, current_user.id, [file.id])
    return FileResponse(**add_urls_to_file(file, progress_positions.get(file.id, 0)))


@router.delete("/{file_id}")
async def delete_file(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Move a file to Recycle Bin, or permanently delete when it is disabled."""
    count, recycled = await trash_files(db, current_user.id, [file_id])
    if not count:
        raise HTTPException(status_code=404, detail="File not found")
    await db.commit()
    return {
        "message": "File moved to Recycle Bin" if recycled else "File permanently deleted",
        "recycled": recycled,
    }


@router.post("/batch-delete")
async def batch_delete_files(
    request: BatchIdsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Move multiple files to Recycle Bin, or permanently delete when disabled."""
    file_ids = request.root
    count, recycled = await trash_files(db, current_user.id, file_ids)
    if not count:
        return {"message": "No files found to delete"}
    await db.commit()
    return {
        "message": (
            f"Moved {count} files to Recycle Bin"
            if recycled else f"Permanently deleted {count} files"
        ),
        "recycled": recycled,
    }


@router.post("/{file_id}/progress")
@router.put("/{file_id}/progress")
async def update_progress(
    file_id: int,
    progress: WatchProgressUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update watch progress. Supports both POST and PUT."""
    # Check file exists
    result = await db.execute(
        select(File)
        .where(File.id == file_id, File.user_id == current_user.id)
        .execution_options(include_deleted=True)
    )
    file = result.scalar_one_or_none()
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    duration_value = int(progress.duration) if progress.duration else None
    position_value = max(0, int(progress.position or 0))
    completed = bool(
        duration_value
        and duration_value > 0
        and position_value >= int(duration_value * 0.95)
    )

    # Position 0 or completed means remove it from Continue Watching.
    # This prevents finished files from reopening at an old last_pos.
    if position_value <= 0 or completed:
        await db.execute(
            delete(WatchProgress).where(
                WatchProgress.file_id == file_id,
                WatchProgress.user_id == current_user.id,
            )
        )
        await db.commit()
        return {
            "file_id": file_id,
            "position": 0,
            "duration": duration_value,
            "completed": True,
        }

    # Get or create progress
    result = await db.execute(
        select(WatchProgress).where(WatchProgress.file_id == file_id, WatchProgress.user_id == current_user.id)
    )
    watch_progress = result.scalar_one_or_none()

    if not watch_progress:
        watch_progress = WatchProgress(
            user_id=current_user.id,
            file_id=file_id,
            position=position_value,
            duration=duration_value,
            completed=False,
        )
        db.add(watch_progress)
    else:
        watch_progress.position = position_value
        watch_progress.completed = False
        if duration_value:
            watch_progress.duration = duration_value

    await db.commit()
    await db.refresh(watch_progress)
    return watch_progress


@router.get("/{file_id}/progress")
async def get_progress(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get watch progress for a file."""
    result = await db.execute(
        select(WatchProgress).where(
            WatchProgress.file_id == file_id, 
            WatchProgress.user_id == current_user.id
        )
    )
    progress = result.scalar_one_or_none()
    
    if not progress:
        return {
            "id": 0,
            "user_id": current_user.id,
            "file_id": file_id,
            "position": 0,
            "duration": 0,
            "completed": False,
            "updated_at": None,
        }
    
    return {
        "id": progress.id,
        "user_id": progress.user_id,
        "file_id": progress.file_id,
        "position": progress.position,
        "duration": progress.duration or 0,
        "completed": progress.completed,
        "updated_at": progress.updated_at,
    }


@router.post("/{file_id}/share", response_model=FileResponse)
async def share_file(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate a permanent public link for the file."""
    result = await db.execute(
        select(File).where(File.id == file_id, File.user_id == current_user.id)
    )
    file = result.scalar_one_or_none()
    
    if not file:
        raise HTTPException(status_code=404, detail="File not found")
    
    # Generate hash if not exists or regenerate
    # Using 16 bytes = 32 hex chars
    file.public_hash = secrets.token_hex(16)
    
    await db.commit()
    
    # Re-fetch with relationships
    result = await db.execute(
        select(File).where(File.id == file_id, File.user_id == current_user.id)
    )
    file = result.scalar_one()
    
    progress_positions = await fetch_progress_positions(db, current_user.id, [file.id])
    return FileResponse(**add_urls_to_file(file, progress_positions.get(file.id, 0)))


@router.delete("/{file_id}/share", response_model=FileResponse)
async def revoke_share(
    file_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Revoke the public link for the file."""
    result = await db.execute(
        select(File).where(File.id == file_id, File.user_id == current_user.id)
    )
    file = result.scalar_one_or_none()
    
    if not file:
        raise HTTPException(status_code=404, detail="File not found")
    
    file.public_hash = None
    
    await db.commit()
    
    # Re-fetch with relationships
    result = await db.execute(
        select(File).where(File.id == file_id, File.user_id == current_user.id)
    )
    file = result.scalar_one()
    
    progress_positions = await fetch_progress_positions(db, current_user.id, [file.id])
    return FileResponse(**add_urls_to_file(file, progress_positions.get(file.id, 0)))


@router.post("/batch-move")
async def batch_move_files(
    move_data: BatchMoveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Move multiple files to a folder."""
    file_ids = move_data.ids
    folder_id = move_data.folder_id
    
    if folder_id == 0:
        folder_id = None
        
    # Verify target folder belongs to user
    if folder_id is not None:
        from ..models import Folder
        folder_check = await db.execute(
            select(Folder).where(Folder.id == folder_id, Folder.user_id == current_user.id)
        )
        if not folder_check.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Target folder not found")
            
    # Update files
    from sqlalchemy import update
    await db.execute(
        update(File)
        .where(File.id.in_(file_ids), File.user_id == current_user.id)
        .values(folder_id=folder_id)
    )
    
    await db.commit()
    return {"message": f"Moved {len(file_ids)} files"}
