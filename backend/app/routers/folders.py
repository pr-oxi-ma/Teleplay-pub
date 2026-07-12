"""
Folder management API endpoints.
"""
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, RootModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text, update

from ..database import get_db
from ..models import Folder, File, User
from ..schemas import FolderResponse, FolderCreate, FolderUpdate, FolderWithChildren
from ..auth import get_current_user
from ..recycle_bin import trash_folder, trash_folders
from ..services import sanitize_filename


router = APIRouter(prefix="/folders", tags=["Folders"])


MAX_BATCH_IDS = 500


class BatchIdsRequest(RootModel[list[int]]):
    root: list[int] = Field(..., min_length=1, max_length=MAX_BATCH_IDS)


class BatchMoveRequest(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=MAX_BATCH_IDS)
    folder_id: Optional[int] = None


async def get_folder_file_count(db: AsyncSession, folder_id: int) -> int:
    """Get the total number of files in a folder and all its subfolders."""
    # This is a recursive CTE approach for efficiency.
    query = text("""
        WITH RECURSIVE subfolders AS (
            SELECT id FROM folders WHERE id = :root_id AND deleted_at IS NULL
            UNION ALL
            SELECT f.id FROM folders f
            INNER JOIN subfolders sf ON f.parent_id = sf.id
            WHERE f.deleted_at IS NULL
        )
        SELECT COUNT(*) FROM files
        WHERE folder_id IN (SELECT id FROM subfolders) AND deleted_at IS NULL
    """)
    result = await db.execute(query, {"root_id": folder_id})
    return result.scalar() or 0


def clean_folder_name(name: str) -> str:
    """Normalize user-visible folder names and block path-like names."""
    cleaned = sanitize_filename(name).strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Folder name cannot be empty")
    return cleaned


async def get_descendant_folder_ids(
    db: AsyncSession,
    folder_id: int,
    user_id: int,
) -> list[int]:
    """Return folder_id plus every descendant folder owned by the user."""
    query = text("""
        WITH RECURSIVE subfolders AS (
            SELECT id FROM folders WHERE id = :root_id AND user_id = :user_id AND deleted_at IS NULL
            UNION ALL
            SELECT f.id FROM folders f
            INNER JOIN subfolders sf ON f.parent_id = sf.id
            WHERE f.user_id = :user_id AND f.deleted_at IS NULL
        )
        SELECT id FROM subfolders
    """)
    result = await db.execute(query, {"root_id": folder_id, "user_id": user_id})
    return [int(item) for item in result.scalars().all()]


async def ensure_target_parent_is_valid(
    db: AsyncSession,
    target_parent_id: Optional[int],
    current_user: User,
    moving_folder_ids: list[int],
) -> None:
    """Validate a folder move target and prevent recursive/cyclic folders."""
    if target_parent_id is None:
        return

    parent_check = await db.execute(
        select(Folder.id).where(
            Folder.id == target_parent_id,
            Folder.user_id == current_user.id,
        )
    )
    if parent_check.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Target parent folder not found")

    blocked_ids: set[int] = set()
    for moving_id in moving_folder_ids:
        blocked_ids.update(await get_descendant_folder_ids(db, moving_id, current_user.id))

    if target_parent_id in blocked_ids:
        raise HTTPException(
            status_code=400,
            detail="Cannot move a folder into itself or one of its subfolders",
        )


async def ensure_unique_folder_name(
    db: AsyncSession,
    current_user: User,
    folder_name: str,
    parent_id: Optional[int],
    exclude_folder_id: Optional[int] = None,
) -> None:
    stmt = select(Folder.id).where(
        Folder.user_id == current_user.id,
        Folder.name == folder_name,
    )
    if parent_id is None:
        stmt = stmt.where(Folder.parent_id.is_(None))
    else:
        stmt = stmt.where(Folder.parent_id == parent_id)
    if exclude_folder_id is not None:
        stmt = stmt.where(Folder.id != exclude_folder_id)

    existing = await db.execute(stmt)
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=400,
            detail="Folder with this name already exists in the target folder",
        )


@router.get("", response_model=List[FolderResponse])
async def list_folders(
    parent_id: Optional[int] = Query(None, description="Filter by parent folder ID"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List user's folders optimally."""
    # Select folder and count of files in one query
    stmt = (
        select(Folder, func.count(File.id).label("file_count"))
        .outerjoin(File, File.folder_id == Folder.id)
        .where(Folder.user_id == current_user.id)
        .group_by(Folder.id)
        .order_by(Folder.name)
    )
    
    if parent_id is not None:
        stmt = stmt.where(Folder.parent_id == parent_id)
    else:
        stmt = stmt.where(Folder.parent_id.is_(None))
    
    result = await db.execute(stmt)
    rows = result.all()
    
    return [
        FolderResponse(
            id=folder.id,
            name=folder.name,
            parent_id=folder.parent_id,
            user_id=folder.user_id,
            created_at=folder.created_at,
            updated_at=folder.updated_at,
            file_count=file_count
        )
        for folder, file_count in rows
    ]


@router.get("/tree", response_model=List[FolderWithChildren])
async def get_folder_tree(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get the complete folder tree for the user optimally."""
    # Get all folders with counts in one query
    stmt = (
        select(Folder, func.count(File.id).label("file_count"))
        .outerjoin(File, File.folder_id == Folder.id)
        .where(Folder.user_id == current_user.id)
        .group_by(Folder.id)
        .order_by(Folder.name)
    )
    
    result = await db.execute(stmt)
    rows = result.all()
    
    # Build tree
    folder_map = {}
    for folder, file_count in rows:
        folder_map[folder.id] = {
            "id": folder.id,
            "name": folder.name,
            "parent_id": folder.parent_id,
            "user_id": folder.user_id,
            "created_at": folder.created_at,
            "updated_at": folder.updated_at,
            "file_count": file_count,
            "children": [],
        }
    
    # Link parents and children
    roots = []
    for folder_data in folder_map.values():
        parent_id = folder_data["parent_id"]
        if parent_id and parent_id in folder_map:
            folder_map[parent_id]["children"].append(folder_data)
        else:
            roots.append(folder_data)
    
    return roots


@router.get("/{folder_id}", response_model=FolderResponse)
async def get_folder(
    folder_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a specific folder by ID with file count."""
    stmt = (
        select(Folder, func.count(File.id).label("file_count"))
        .outerjoin(File, File.folder_id == Folder.id)
        .where(Folder.id == folder_id, Folder.user_id == current_user.id)
        .group_by(Folder.id)
    )
    
    result = await db.execute(stmt)
    row = result.first()
    
    if not row:
        raise HTTPException(status_code=404, detail="Folder not found")
    
    folder, file_count = row
    
    return FolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        user_id=folder.user_id,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
        file_count=file_count,
    )


@router.post("", response_model=FolderResponse, status_code=201)
async def create_folder(
    folder_data: FolderCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new folder."""
    parent_id = folder_data.parent_id
    if parent_id == 0:
        parent_id = None

    # Validate parent exists if specified
    if parent_id is not None:
        parent_result = await db.execute(
            select(Folder).where(
                Folder.id == parent_id,
                Folder.user_id == current_user.id
            )
        )
        if not parent_result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Parent folder not found")
    
    folder_name = clean_folder_name(folder_data.name)
    await ensure_unique_folder_name(db, current_user, folder_name, parent_id)
    
    folder = Folder(
        user_id=current_user.id,
        name=folder_name,
        parent_id=parent_id,
    )
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    
    return FolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        user_id=folder.user_id,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
        file_count=0,
    )


@router.patch("/{folder_id}", response_model=FolderResponse)
async def update_folder(
    folder_id: int,
    update_data: FolderUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update a folder (rename, move)."""
    result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == current_user.id)
    )
    folder = result.scalar_one_or_none()
    
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    
    fields_set = update_data.model_fields_set
    target_parent_id = folder.parent_id

    if "parent_id" in fields_set:
        target_parent_id = update_data.parent_id
        if target_parent_id == 0:
            target_parent_id = None
        await ensure_target_parent_is_valid(db, target_parent_id, current_user, [folder_id])

    folder_name = folder.name
    if "name" in fields_set and update_data.name is not None:
        folder_name = clean_folder_name(update_data.name)

    if folder_name != folder.name or target_parent_id != folder.parent_id:
        await ensure_unique_folder_name(
            db,
            current_user,
            folder_name,
            target_parent_id,
            exclude_folder_id=folder_id,
        )

    folder.name = folder_name
    folder.parent_id = target_parent_id
    
    await db.commit()
    await db.refresh(folder)
    
    file_count = await get_folder_file_count(db, folder.id)
    
    return FolderResponse(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        user_id=folder.user_id,
        created_at=folder.created_at,
        updated_at=folder.updated_at,
        file_count=file_count,
    )


@router.delete("/{folder_id}")
async def delete_folder(
    folder_id: int,
    move_files_to: Optional[int] = Query(None, description="Move files to this folder ID (null = root)"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Move a folder hierarchy to Recycle Bin, or permanently delete when disabled."""
    result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == current_user.id)
    )
    folder = result.scalar_one_or_none()
    
    if not folder:
        raise HTTPException(status_code=404, detail="Folder not found")
    
    # Backward-compatible explicit move: move files out before trashing the now
    # empty folder hierarchy.
    if move_files_to is not None:
        folder_ids = await get_descendant_folder_ids(db, folder_id, current_user.id)
        target_folder_id = move_files_to if move_files_to != 0 else None
        await ensure_target_parent_is_valid(db, target_folder_id, current_user, [folder_id])

        if folder_ids:
            await db.execute(
                update(File)
                .where(
                    File.user_id == current_user.id,
                    File.folder_id.in_(folder_ids),
                )
                .values(folder_id=target_folder_id)
            )
    folders_count, files_count, recycled = await trash_folder(
        db, current_user.id, folder_id
    )
    await db.commit()
    return {
        "message": (
            "Folder moved to Recycle Bin" if recycled else "Folder permanently deleted"
        ),
        "recycled": recycled,
        "folders": folders_count,
        "files": files_count,
    }


@router.post("/batch-delete")
async def batch_delete_folders(
    request: BatchIdsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Move multiple folder hierarchies to Recycle Bin."""
    folder_ids = request.root
    folders_count, files_count, recycled = await trash_folders(
        db, current_user.id, folder_ids
    )
    if not folders_count:
        return {"message": "No folders found to delete"}
    await db.commit()
    return {
        "message": (
            f"Moved {folders_count} folders to Recycle Bin"
            if recycled else f"Permanently deleted {folders_count} folders"
        ),
        "recycled": recycled,
        "folders": folders_count,
        "files": files_count,
    }


@router.post("/batch-move")
async def batch_move_folders(
    move_data: BatchMoveRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Move multiple folders to another folder."""
    folder_ids = move_data.ids
    target_id = move_data.folder_id
    
    if target_id == 0:
        target_id = None
        
    if not folder_ids:
        return {"message": "No folders to move"}

    await ensure_target_parent_is_valid(db, target_id, current_user, folder_ids)
            
    # Update folders
    await db.execute(
        update(Folder)
        .where(Folder.id.in_(folder_ids), Folder.user_id == current_user.id)
        .values(parent_id=target_id)
    )
    
    await db.commit()
    return {"message": f"Moved {len(folder_ids)} folders"}
