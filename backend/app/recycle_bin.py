"""Recycle-bin business logic shared by API routes, bot actions and cleanup."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from .database import async_session
from .models import File, Folder, UserSettings, WatchProgress
from .telegram import delete_from_storage_channel
from .thumbnail_cache import delete_cached_thumbnail
from .time_utils import utcnow

logger = logging.getLogger(__name__)
PURGE_INTERVAL_SECONDS = 6 * 60 * 60


async def get_or_create_recycle_settings(db: AsyncSession, user_id: int) -> UserSettings:
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    preferences = result.scalar_one_or_none()
    if preferences is None:
        preferences = UserSettings(user_id=user_id)
        db.add(preferences)
        await db.flush()
    return preferences


async def get_descendant_folder_ids(
    db: AsyncSession,
    folder_id: int,
    user_id: int,
    *,
    include_deleted: bool = False,
) -> list[int]:
    active_clause = "" if include_deleted else "AND deleted_at IS NULL"
    child_active_clause = "" if include_deleted else "AND f.deleted_at IS NULL"
    query = text(f"""
        WITH RECURSIVE subfolders AS (
            SELECT id FROM folders
            WHERE id = :root_id AND user_id = :user_id {active_clause}
            UNION ALL
            SELECT f.id FROM folders f
            INNER JOIN subfolders sf ON f.parent_id = sf.id
            WHERE f.user_id = :user_id {child_active_clause}
        )
        SELECT id FROM subfolders
    """)
    result = await db.execute(query, {"root_id": folder_id, "user_id": user_id})
    return [int(value) for value in result.scalars().all()]


async def _delete_messages(message_ids: list[int]) -> None:
    for offset in range(0, len(message_ids), 100):
        deleted = await delete_from_storage_channel(message_ids[offset:offset + 100])
        if not deleted:
            raise RuntimeError("Telegram storage deletion failed; database purge was cancelled")


async def permanently_delete_files(
    db: AsyncSession,
    user_id: int,
    file_ids: list[int],
    *,
    trashed_only: bool = False,
) -> int:
    if not file_ids:
        return 0
    stmt = select(File).where(File.user_id == user_id, File.id.in_(set(file_ids)))
    if trashed_only:
        stmt = stmt.where(File.deleted_at.is_not(None))
    result = await db.execute(stmt.execution_options(include_deleted=True))
    files = result.scalars().all()
    if not files:
        return 0

    await _delete_messages([int(item.channel_message_id) for item in files if item.channel_message_id])
    ids = [item.id for item in files]
    for item in files:
        delete_cached_thumbnail(item)
    await db.execute(delete(WatchProgress).where(WatchProgress.file_id.in_(ids)))
    await db.execute(delete(File).where(File.id.in_(ids), File.user_id == user_id))
    return len(ids)


async def permanently_delete_folder(
    db: AsyncSession,
    user_id: int,
    folder_id: int,
    *,
    trashed_only: bool = False,
) -> tuple[int, int]:
    stmt = select(Folder).where(Folder.id == folder_id, Folder.user_id == user_id)
    if trashed_only:
        stmt = stmt.where(Folder.deleted_at.is_not(None))
    root_result = await db.execute(stmt.execution_options(include_deleted=True))
    root = root_result.scalar_one_or_none()
    if root is None:
        return 0, 0

    descendant_ids = await get_descendant_folder_ids(
        db, folder_id, user_id, include_deleted=trashed_only
    )
    if trashed_only:
        trash_root_id = root.trash_root_id
        folder_result = await db.execute(
            select(Folder.id)
            .where(
                Folder.user_id == user_id,
                Folder.id.in_(descendant_ids),
                Folder.deleted_at.is_not(None),
                Folder.trash_root_id == trash_root_id,
            )
            .execution_options(include_deleted=True)
        )
        folder_ids = [int(value) for value in folder_result.scalars().all()]
    else:
        folder_ids = descendant_ids

    if not folder_ids:
        return 0, 0

    file_stmt = select(File).where(
        File.user_id == user_id,
        File.folder_id.in_(folder_ids),
    )
    if trashed_only:
        file_stmt = file_stmt.where(
            File.deleted_at.is_not(None),
            File.trash_root_id == root.trash_root_id,
        )
    file_result = await db.execute(file_stmt.execution_options(include_deleted=True))
    files = file_result.scalars().all()
    file_ids = [item.id for item in files]
    await _delete_messages([int(item.channel_message_id) for item in files if item.channel_message_id])
    for item in files:
        delete_cached_thumbnail(item)

    if file_ids:
        await db.execute(delete(WatchProgress).where(WatchProgress.file_id.in_(file_ids)))
        await db.execute(delete(File).where(File.id.in_(file_ids), File.user_id == user_id))
    await db.execute(
        delete(Folder).where(Folder.id.in_(folder_ids), Folder.user_id == user_id)
    )
    return len(folder_ids), len(file_ids)


async def _active_folder_chain(
    db: AsyncSession,
    user_id: int,
    folder_id: int,
) -> list[Folder]:
    """Return the active path from its highest visible ancestor to folder_id."""
    result = await db.execute(select(Folder).where(Folder.user_id == user_id))
    active_by_id = {int(item.id): item for item in result.scalars().all()}
    chain: list[Folder] = []
    current_id: int | None = folder_id
    visited: set[int] = set()
    while current_id is not None and current_id not in visited:
        visited.add(current_id)
        current = active_by_id.get(current_id)
        if current is None:
            return []
        chain.append(current)
        current_id = current.parent_id
    chain.reverse()
    return chain


async def _deleted_child_by_name(
    db: AsyncSession,
    user_id: int,
    parent_id: int,
    name: str,
    trash_root_id: int,
) -> Folder | None:
    result = await db.execute(
        select(Folder)
        .where(
            Folder.user_id == user_id,
            Folder.parent_id == parent_id,
            Folder.name == name,
            Folder.deleted_at.is_not(None),
            Folder.trash_root_id == trash_root_id,
        )
        .order_by(Folder.id.asc())
        .execution_options(include_deleted=True)
    )
    return result.scalars().first()


async def _refresh_trash_group_retention(
    db: AsyncSession,
    user_id: int,
    trash_root_id: int,
    deleted_at,
    purge_after,
) -> None:
    """Treat content merged back into a trash folder as a fresh deletion.

    A partially restored item should get the full retention window when it is
    deleted again. Refreshing the complete group also keeps the folder card and
    all nested entries on one consistent expiry date.
    """
    await db.execute(
        update(Folder)
        .where(
            Folder.user_id == user_id,
            Folder.deleted_at.is_not(None),
            Folder.trash_root_id == trash_root_id,
        )
        .values(deleted_at=deleted_at, purge_after=purge_after)
    )
    await db.execute(
        update(File)
        .where(
            File.user_id == user_id,
            File.deleted_at.is_not(None),
            File.trash_root_id == trash_root_id,
        )
        .values(deleted_at=deleted_at, purge_after=purge_after)
    )


async def _merge_trashed_folder_into_target(
    db: AsyncSession,
    user_id: int,
    source: Folder,
    target: Folder,
    source_root_id: int,
    target_root_id: int,
) -> None:
    """Merge one already-trashed folder tree into another without duplicates."""
    await db.execute(
        update(File)
        .where(
            File.user_id == user_id,
            File.folder_id == source.id,
            File.deleted_at.is_not(None),
            File.trash_root_id == source_root_id,
        )
        .values(folder_id=target.id, trash_root_id=target_root_id)
    )

    child_result = await db.execute(
        select(Folder)
        .where(
            Folder.user_id == user_id,
            Folder.parent_id == source.id,
            Folder.deleted_at.is_not(None),
            Folder.trash_root_id == source_root_id,
        )
        .order_by(Folder.id.asc())
        .execution_options(include_deleted=True)
    )
    children = list(child_result.scalars().all())
    for child in children:
        matching = await _deleted_child_by_name(
            db, user_id, int(target.id), child.name, target_root_id
        )
        if matching is not None:
            await _merge_trashed_folder_into_target(
                db,
                user_id,
                child,
                matching,
                source_root_id,
                target_root_id,
            )
            continue

        subtree_ids = await get_descendant_folder_ids(
            db, int(child.id), user_id, include_deleted=True
        )
        child.parent_id = int(target.id)
        if subtree_ids:
            await db.execute(
                update(Folder)
                .where(
                    Folder.user_id == user_id,
                    Folder.id.in_(subtree_ids),
                    Folder.deleted_at.is_not(None),
                    Folder.trash_root_id == source_root_id,
                )
                .values(trash_root_id=target_root_id)
            )
            await db.execute(
                update(File)
                .where(
                    File.user_id == user_id,
                    File.folder_id.in_(subtree_ids),
                    File.deleted_at.is_not(None),
                    File.trash_root_id == source_root_id,
                )
                .values(trash_root_id=target_root_id)
            )

    await db.flush()
    await db.execute(
        delete(Folder).where(
            Folder.id == source.id,
            Folder.user_id == user_id,
            Folder.deleted_at.is_not(None),
        )
    )


async def merge_duplicate_trash_roots(db: AsyncSession, user_id: int) -> int:
    """Coalesce duplicate cards that represent the same original folder path.

    Active folders are unique by name within a parent. Therefore two deleted
    root folders with the same parent/name represent the same logical path. The
    old partial-restore flow could create exactly this duplicate. Keeping one
    canonical root also makes repeated restore/delete cycles idempotent.
    """
    result = await db.execute(
        select(Folder)
        .where(
            Folder.user_id == user_id,
            Folder.deleted_at.is_not(None),
            Folder.trash_root_id == Folder.id,
        )
        .order_by(Folder.id.asc())
        .execution_options(include_deleted=True)
    )
    roots = list(result.scalars().all())
    grouped: dict[tuple[int | None, str], list[Folder]] = {}
    for root in roots:
        grouped.setdefault((root.parent_id, root.name), []).append(root)

    merged = 0
    for group in grouped.values():
        if len(group) < 2:
            continue
        canonical = group[0]
        effective_deleted_at = max(
            (item.deleted_at for item in group if item.deleted_at is not None),
            default=canonical.deleted_at,
        )
        effective_purge_after = max(
            (item.purge_after for item in group if item.purge_after is not None),
            default=canonical.purge_after,
        )
        for duplicate in group[1:]:
            await _merge_trashed_folder_into_target(
                db,
                user_id,
                duplicate,
                canonical,
                int(duplicate.trash_root_id or duplicate.id),
                int(canonical.id),
            )
            merged += 1

        if effective_deleted_at is not None and effective_purge_after is not None:
            await _refresh_trash_group_retention(
                db,
                user_id,
                int(canonical.id),
                effective_deleted_at,
                effective_purge_after,
            )
    return merged


async def _find_or_create_trash_destination_for_active_folder(
    db: AsyncSession,
    user_id: int,
    active_folder_id: int,
    deleted_at,
    purge_after,
) -> tuple[Folder, Folder] | None:
    """Map an active restored path back to its existing Recycle Bin tree.

    Matching is path-based rather than ID-based because partial restore creates
    active folder rows while the original deleted rows must remain in the bin.
    Missing nested folders are created only as lightweight metadata containers.
    """
    chain = await _active_folder_chain(db, user_id, active_folder_id)
    if not chain:
        return None

    root_result = await db.execute(
        select(Folder)
        .where(
            Folder.user_id == user_id,
            Folder.deleted_at.is_not(None),
            Folder.trash_root_id == Folder.id,
        )
        .order_by(Folder.id.asc())
        .execution_options(include_deleted=True)
    )
    roots = list(root_result.scalars().all())

    # Prefer the deepest matching component. For /Parent/P/Child this chooses P
    # instead of an older deleted /Parent tree when both happen to exist.
    for index in range(len(chain) - 1, -1, -1):
        component = chain[index]
        root = next(
            (
                item
                for item in roots
                if item.name == component.name and item.parent_id == component.parent_id
            ),
            None,
        )
        if root is None:
            continue

        target = root
        for descendant in chain[index + 1:]:
            child = await _deleted_child_by_name(
                db, user_id, int(target.id), descendant.name, int(root.id)
            )
            if child is None:
                child = Folder(
                    user_id=user_id,
                    parent_id=int(target.id),
                    name=descendant.name,
                    deleted_at=deleted_at,
                    purge_after=purge_after,
                    trash_root_id=int(root.id),
                )
                db.add(child)
                await db.flush()
            target = child
        return target, root
    return None


async def _mark_active_subtree_deleted_in_place(
    db: AsyncSession,
    user_id: int,
    folder: Folder,
    target_parent_id: int,
    trash_root_id: int,
    deleted_at,
    purge_after,
) -> None:
    subtree_ids = await get_descendant_folder_ids(db, int(folder.id), user_id)
    if not subtree_ids:
        return
    folder.parent_id = target_parent_id
    await db.execute(
        update(Folder)
        .where(
            Folder.user_id == user_id,
            Folder.id.in_(subtree_ids),
            Folder.deleted_at.is_(None),
        )
        .values(
            deleted_at=deleted_at,
            purge_after=purge_after,
            trash_root_id=trash_root_id,
        )
    )
    await db.execute(
        update(File)
        .where(
            File.user_id == user_id,
            File.folder_id.in_(subtree_ids),
            File.deleted_at.is_(None),
        )
        .values(
            deleted_at=deleted_at,
            purge_after=purge_after,
            trash_root_id=trash_root_id,
            public_hash=None,
        )
    )


async def _merge_active_folder_into_trash_target(
    db: AsyncSession,
    user_id: int,
    source: Folder,
    target: Folder,
    trash_root_id: int,
    deleted_at,
    purge_after,
) -> None:
    """Move an active folder tree into an existing deleted folder tree."""
    await db.execute(
        update(File)
        .where(
            File.user_id == user_id,
            File.folder_id == source.id,
            File.deleted_at.is_(None),
        )
        .values(
            folder_id=target.id,
            deleted_at=deleted_at,
            purge_after=purge_after,
            trash_root_id=trash_root_id,
            public_hash=None,
        )
    )

    child_result = await db.execute(
        select(Folder)
        .where(
            Folder.user_id == user_id,
            Folder.parent_id == source.id,
            Folder.deleted_at.is_(None),
        )
        .order_by(Folder.id.asc())
    )
    children = list(child_result.scalars().all())
    for child in children:
        matching = await _deleted_child_by_name(
            db, user_id, int(target.id), child.name, trash_root_id
        )
        if matching is not None:
            await _merge_active_folder_into_trash_target(
                db,
                user_id,
                child,
                matching,
                trash_root_id,
                deleted_at,
                purge_after,
            )
        else:
            await _mark_active_subtree_deleted_in_place(
                db,
                user_id,
                child,
                int(target.id),
                trash_root_id,
                deleted_at,
                purge_after,
            )

    await db.flush()
    await db.execute(
        delete(Folder).where(
            Folder.id == source.id,
            Folder.user_id == user_id,
            Folder.deleted_at.is_(None),
        )
    )


async def trash_files(db: AsyncSession, user_id: int, file_ids: list[int]) -> tuple[int, bool]:
    preferences = await get_or_create_recycle_settings(db, user_id)
    unique_ids = list(dict.fromkeys(file_ids))
    if not preferences.recycle_bin_enabled:
        count = await permanently_delete_files(db, user_id, unique_ids)
        return count, False

    await merge_duplicate_trash_roots(db, user_id)
    result = await db.execute(
        select(File).where(File.user_id == user_id, File.id.in_(unique_ids))
    )
    files = result.scalars().all()
    now = utcnow()
    purge_after = now + timedelta(days=preferences.recycle_bin_retention_days)
    destination_cache: dict[int, tuple[Folder, Folder] | None] = {}
    touched_roots: set[int] = set()
    for item in files:
        destination: tuple[Folder, Folder] | None = None
        if item.folder_id is not None:
            folder_id = int(item.folder_id)
            if folder_id not in destination_cache:
                destination_cache[folder_id] = await _find_or_create_trash_destination_for_active_folder(
                    db, user_id, folder_id, now, purge_after
                )
            destination = destination_cache[folder_id]

        if destination is not None:
            target, root = destination
            item.folder_id = int(target.id)
            item.trash_root_id = int(root.id)
            touched_roots.add(int(root.id))
        else:
            item.trash_root_id = None
        item.deleted_at = now
        item.purge_after = purge_after
        item.public_hash = None

    await db.flush()
    for root_id in touched_roots:
        await _refresh_trash_group_retention(
            db, user_id, root_id, now, purge_after
        )
    return len(files), True


async def trash_folder(
    db: AsyncSession,
    user_id: int,
    folder_id: int,
) -> tuple[int, int, bool]:
    preferences = await get_or_create_recycle_settings(db, user_id)
    folder_ids = await get_descendant_folder_ids(db, folder_id, user_id)
    if not folder_ids:
        return 0, 0, preferences.recycle_bin_enabled

    if not preferences.recycle_bin_enabled:
        folders_deleted, files_deleted = await permanently_delete_folder(
            db, user_id, folder_id
        )
        return folders_deleted, files_deleted, False

    await merge_duplicate_trash_roots(db, user_id)
    now = utcnow()
    purge_after = now + timedelta(days=preferences.recycle_bin_retention_days)
    destination = await _find_or_create_trash_destination_for_active_folder(
        db, user_id, folder_id, now, purge_after
    )
    if destination is not None:
        source_result = await db.execute(
            select(Folder).where(Folder.id == folder_id, Folder.user_id == user_id)
        )
        source = source_result.scalar_one_or_none()
        if source is None:
            return 0, 0, True
        files_count = (
            await db.execute(
                select(func.count(File.id)).where(
                    File.user_id == user_id,
                    File.folder_id.in_(folder_ids),
                    File.deleted_at.is_(None),
                )
            )
        ).scalar() or 0
        target, root = destination
        await _merge_active_folder_into_trash_target(
            db,
            user_id,
            source,
            target,
            int(root.id),
            now,
            purge_after,
        )
        await _refresh_trash_group_retention(
            db, user_id, int(root.id), now, purge_after
        )
        return len(folder_ids), int(files_count), True

    folder_result = await db.execute(
        update(Folder)
        .where(
            Folder.user_id == user_id,
            Folder.id.in_(folder_ids),
            Folder.deleted_at.is_(None),
        )
        .values(deleted_at=now, purge_after=purge_after, trash_root_id=folder_id)
    )
    file_result = await db.execute(
        update(File)
        .where(
            File.user_id == user_id,
            File.folder_id.in_(folder_ids),
            File.deleted_at.is_(None),
        )
        .values(
            deleted_at=now,
            purge_after=purge_after,
            trash_root_id=folder_id,
            public_hash=None,
        )
    )
    return int(folder_result.rowcount or 0), int(file_result.rowcount or 0), True


async def trash_folders(
    db: AsyncSession, user_id: int, folder_ids: list[int]
) -> tuple[int, int, bool]:
    selected = set(folder_ids)
    if not selected:
        return 0, 0, True

    all_result = await db.execute(
        select(Folder.id, Folder.parent_id).where(Folder.user_id == user_id)
    )
    parent_by_id = {int(row.id): row.parent_id for row in all_result.all()}
    roots: list[int] = []
    for folder_id in selected:
        parent_id = parent_by_id.get(folder_id)
        has_selected_ancestor = False
        visited: set[int] = set()
        while parent_id is not None and parent_id not in visited:
            if parent_id in selected:
                has_selected_ancestor = True
                break
            visited.add(parent_id)
            parent_id = parent_by_id.get(parent_id)
        if not has_selected_ancestor and folder_id in parent_by_id:
            roots.append(folder_id)

    folders_count = files_count = 0
    used_recycle_bin = True
    for root_id in roots:
        folder_count, file_count, recycled = await trash_folder(db, user_id, root_id)
        folders_count += folder_count
        files_count += file_count
        used_recycle_bin = used_recycle_bin and recycled
    return folders_count, files_count, used_recycle_bin


async def _find_or_create_active_folder(
    db: AsyncSession,
    user_id: int,
    parent_id: int | None,
    name: str,
) -> Folder:
    stmt = select(Folder).where(
        Folder.user_id == user_id,
        Folder.name == name,
    )
    stmt = stmt.where(
        Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id
    )
    existing = (await db.execute(stmt)).scalars().first()
    if existing is not None:
        return existing

    folder = Folder(user_id=user_id, parent_id=parent_id, name=name)
    db.add(folder)
    await db.flush()
    return folder


async def _active_parent_id(
    db: AsyncSession,
    user_id: int,
    parent_id: int | None,
) -> int | None:
    if parent_id is None:
        return None
    result = await db.execute(
        select(Folder.id).where(Folder.id == parent_id, Folder.user_id == user_id)
    )
    value = result.scalar_one_or_none()
    return int(value) if value is not None else None


async def _trash_group_folder_map(
    db: AsyncSession,
    user_id: int,
    trash_root_id: int,
) -> dict[int, Folder]:
    result = await db.execute(
        select(Folder)
        .where(
            Folder.user_id == user_id,
            Folder.deleted_at.is_not(None),
            Folder.trash_root_id == trash_root_id,
        )
        .execution_options(include_deleted=True)
    )
    return {int(folder.id): folder for folder in result.scalars().all()}


async def _ensure_active_restore_path(
    db: AsyncSession,
    user_id: int,
    trash_root_id: int,
    original_folder_id: int,
    *,
    active_by_original_id: dict[int, Folder] | None = None,
    group_folders: dict[int, Folder] | None = None,
) -> Folder | None:
    """Recreate/reuse the deleted path without activating the trash metadata.

    This lets one file be restored from a deleted folder while the remaining
    files stay browsable in Recycle Bin.
    """
    active_by_original_id = active_by_original_id if active_by_original_id is not None else {}
    if original_folder_id in active_by_original_id:
        return active_by_original_id[original_folder_id]

    group_folders = group_folders or await _trash_group_folder_map(db, user_id, trash_root_id)
    root = group_folders.get(trash_root_id)
    if root is None:
        return None

    chain: list[Folder] = []
    current_id: int | None = original_folder_id
    visited: set[int] = set()
    while current_id is not None and current_id not in visited:
        visited.add(current_id)
        current = group_folders.get(current_id)
        if current is None:
            return None
        chain.append(current)
        if current.id == trash_root_id:
            break
        current_id = current.parent_id

    if not chain or chain[-1].id != trash_root_id:
        return None

    target_parent_id = await _active_parent_id(db, user_id, root.parent_id)
    for original in reversed(chain):
        cached = active_by_original_id.get(int(original.id))
        if cached is not None:
            target_parent_id = int(cached.id)
            continue
        active = await _find_or_create_active_folder(
            db, user_id, target_parent_id, original.name
        )
        active_by_original_id[int(original.id)] = active
        target_parent_id = int(active.id)

    return active_by_original_id.get(original_folder_id)


async def restore_file(db: AsyncSession, user_id: int, file_id: int) -> bool:
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
        return False

    destination_folder_id: int | None = None
    if item.trash_root_id is not None and item.folder_id is not None:
        destination = await _ensure_active_restore_path(
            db,
            user_id,
            int(item.trash_root_id),
            int(item.folder_id),
        )
        destination_folder_id = int(destination.id) if destination is not None else None
    elif item.folder_id is not None:
        destination_folder_id = await _active_parent_id(db, user_id, int(item.folder_id))

    item.folder_id = destination_folder_id
    item.deleted_at = None
    item.purge_after = None
    item.trash_root_id = None
    return True


async def restore_folder(db: AsyncSession, user_id: int, folder_id: int) -> bool:
    result = await db.execute(
        select(Folder)
        .where(
            Folder.id == folder_id,
            Folder.user_id == user_id,
            Folder.deleted_at.is_not(None),
        )
        .execution_options(include_deleted=True)
    )
    selected_folder = result.scalar_one_or_none()
    if selected_folder is None or selected_folder.trash_root_id is None:
        return False

    trash_root_id = int(selected_folder.trash_root_id)
    group_folders = await _trash_group_folder_map(db, user_id, trash_root_id)
    if folder_id not in group_folders:
        return False

    raw_descendant_ids = await get_descendant_folder_ids(
        db, folder_id, user_id, include_deleted=True
    )
    subtree_ids = [
        folder_id_value
        for folder_id_value in raw_descendant_ids
        if folder_id_value in group_folders
    ]
    if not subtree_ids:
        return False

    active_by_original_id: dict[int, Folder] = {}
    selected_active = await _ensure_active_restore_path(
        db,
        user_id,
        trash_root_id,
        folder_id,
        active_by_original_id=active_by_original_id,
        group_folders=group_folders,
    )
    if selected_active is None:
        return False

    remaining = set(subtree_ids)
    remaining.discard(folder_id)
    while remaining:
        progressed = False
        for original_id in list(remaining):
            original = group_folders[original_id]
            if original.parent_id not in active_by_original_id:
                continue
            parent = active_by_original_id[int(original.parent_id)]
            active_by_original_id[original_id] = await _find_or_create_active_folder(
                db, user_id, int(parent.id), original.name
            )
            remaining.remove(original_id)
            progressed = True
        if not progressed:
            logger.warning("Could not rebuild complete trash subtree for folder %s", folder_id)
            return False

    file_result = await db.execute(
        select(File)
        .where(
            File.user_id == user_id,
            File.deleted_at.is_not(None),
            File.trash_root_id == trash_root_id,
            File.folder_id.in_(subtree_ids),
        )
        .execution_options(include_deleted=True)
    )
    for item in file_result.scalars().all():
        destination = active_by_original_id.get(int(item.folder_id)) if item.folder_id is not None else None
        item.folder_id = int(destination.id) if destination is not None else int(selected_active.id)
        item.deleted_at = None
        item.purge_after = None
        item.trash_root_id = None

    # Persist file moves before removing the deleted folder metadata they pointed to.
    await db.flush()
    await db.execute(
        delete(Folder).where(
            Folder.user_id == user_id,
            Folder.id.in_(subtree_ids),
            Folder.deleted_at.is_not(None),
        )
    )
    return True


async def purge_expired_trash() -> dict[str, int]:
    now = utcnow()
    folders_purged = files_purged = 0
    async with async_session() as db:
        root_result = await db.execute(
            select(Folder.id, Folder.user_id)
            .where(
                Folder.deleted_at.is_not(None),
                Folder.purge_after <= now,
                Folder.trash_root_id == Folder.id,
            )
            .execution_options(include_deleted=True)
        )
        for folder_id, user_id in root_result.all():
            folder_count, file_count = await permanently_delete_folder(
                db, int(user_id), int(folder_id), trashed_only=True
            )
            folders_purged += folder_count
            files_purged += file_count

        file_result = await db.execute(
            select(File.id, File.user_id)
            .where(
                File.deleted_at.is_not(None),
                File.purge_after <= now,
                File.trash_root_id.is_(None),
            )
            .execution_options(include_deleted=True)
        )
        by_user: dict[int, list[int]] = {}
        for file_id, user_id in file_result.all():
            by_user.setdefault(int(user_id), []).append(int(file_id))
        for user_id, file_ids in by_user.items():
            files_purged += await permanently_delete_files(
                db, user_id, file_ids, trashed_only=True
            )
        await db.commit()
    return {"folders": folders_purged, "files": files_purged}


async def recycle_bin_cleanup_loop() -> None:
    while True:
        try:
            purged = await purge_expired_trash()
            if purged["folders"] or purged["files"]:
                logger.info("Recycle-bin cleanup purged %s", purged)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Recycle-bin cleanup failed; it will retry later")
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)
