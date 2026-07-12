"""
TV-specific API endpoints optimized for Android TV clients.
"""
from typing import List
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from sqlalchemy.orm import selectinload

from ..database import get_db
from ..models import File, User, Folder, WatchProgress
from ..auth import get_current_user
from ..config import get_settings
from ..services import (
    escape_like, 
    add_urls_to_file, 
    fetch_recent_files, 
    fetch_continue_watching_files,
    fetch_progress_positions,
)

router = APIRouter(prefix="/tv", tags=["TV"])
settings = get_settings()


def folder_to_dict(folder: Folder, file_count=None) -> dict:
    return {
        "id": folder.id,
        "name": folder.name,
        "parent_id": folder.parent_id,
        "user_id": folder.user_id,
        "created_at": folder.created_at.isoformat() if folder.created_at else None,
        "updated_at": folder.updated_at.isoformat() if folder.updated_at else None,
        "file_count": file_count,
    }


async def files_with_progress(db: AsyncSession, user_id: int, files: list[File]) -> list[dict]:
    positions = await fetch_progress_positions(db, user_id, [file.id for file in files])
    return [add_urls_to_file(file, positions.get(file.id, 0)) for file in files]


@router.get("/browse")
async def tv_browse(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get TV home screen data in a single request.
    Returns continue watching, recent files, and folders.
    Optimized for TV client to minimize API calls.
    """
    # Get continue watching
    continue_watching = await fetch_continue_watching_files(db, current_user.id, 20)
    
    # Get recent files
    recent_files = await fetch_recent_files(db, current_user.id, 20)
    
    # Get top-level folders
    folders_query = (
        select(Folder)
        .where(Folder.user_id == current_user.id, Folder.parent_id == None)
        .order_by(Folder.name)
    )
    folders_result = await db.execute(folders_query)
    folders = folders_result.scalars().all()
    
    return {
        "continue_watching": await files_with_progress(db, current_user.id, continue_watching),
        "recent": await files_with_progress(db, current_user.id, recent_files),
        "folders": [folder_to_dict(f, file_count=None) for f in folders],
    }


@router.get("/continue")
async def tv_continue_watching(
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get continue watching list for TV."""
    files = await fetch_continue_watching_files(db, current_user.id, limit)
    return await files_with_progress(db, current_user.id, files)


@router.get("/recent")
async def tv_recent_files(
    limit: int = Query(20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get recently added files for TV."""
    files = await fetch_recent_files(db, current_user.id, limit)
    return await files_with_progress(db, current_user.id, files)


@router.get("/search")
async def tv_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Search files for TV client."""
    # Search files by name
    files_query = (
        select(File)
        .where(
            File.user_id == current_user.id,
            File.file_name.ilike(f"%{escape_like(q)}%", escape="\\")
        )
        .options(selectinload(File.watch_progress))
        .order_by(desc(File.created_at))
        .limit(limit)
    )
    files_result = await db.execute(files_query)
    files = files_result.scalars().all()
    
    # Search folders by name
    folders_query = (
        select(Folder)
        .where(
            Folder.user_id == current_user.id,
            Folder.name.ilike(f"%{escape_like(q)}%", escape="\\")
        )
        .order_by(Folder.name)
        .limit(20)
    )
    folders_result = await db.execute(folders_query)
    folders = folders_result.scalars().all()
    
    file_results = await files_with_progress(db, current_user.id, files)
    folder_results = [folder_to_dict(f, file_count=None) for f in folders]
    return {
        # Older Android model reads query/results/total. Newer clients can also
        # use files/folders for mixed search results.
        "query": q,
        "results": file_results,
        "total": len(file_results),
        "files": file_results,
        "folders": folder_results,
    }


@router.get("/folder/{folder_id}")
async def tv_folder_detail(
    folder_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get folder details with files and subfolders for TV client.
    Returns folder info, subfolders, files, and parent path for navigation.
    """
    from fastapi import HTTPException
    
    # Get the folder
    folder_result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == current_user.id)
    )
    folder = folder_result.scalar_one_or_none()
    
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    
    # Get subfolders
    subfolders_result = await db.execute(
        select(Folder)
        .where(Folder.user_id == current_user.id, Folder.parent_id == folder_id)
        .order_by(Folder.name)
    )
    subfolders = subfolders_result.scalars().all()
    
    # Get files in this folder
    files_result = await db.execute(
        select(File)
        .where(File.user_id == current_user.id, File.folder_id == folder_id)
        .options(selectinload(File.watch_progress))
        .order_by(File.file_name)
    )
    files = files_result.scalars().all()
    
    # Build parent path for breadcrumb navigation
    parent_path = []
    current_folder = folder
    while current_folder.parent_id:
        parent_result = await db.execute(
            select(Folder).where(Folder.id == current_folder.parent_id)
        )
        parent = parent_result.scalar_one_or_none()
        if parent:
            parent_path.insert(0, {
                "id": parent.id,
                "name": parent.name,
                "parent_id": parent.parent_id,
                "user_id": parent.user_id,
                "created_at": parent.created_at.isoformat() if parent.created_at else None,
                "updated_at": parent.updated_at.isoformat() if parent.updated_at else None,
                "file_count": None,
            })
            current_folder = parent
        else:
            break
    
    return {
        "folder": folder_to_dict(folder, file_count=None),
        "subfolders": [folder_to_dict(sf, file_count=None) for sf in subfolders],
        "files": await files_with_progress(db, current_user.id, files),
        "parent_path": parent_path
    }
