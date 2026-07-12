package com.telegramtv.data.repository

import com.telegramtv.data.api.TelePlayApi
import com.telegramtv.data.model.Folder
import com.telegramtv.data.model.FolderCreateRequest
import com.telegramtv.data.model.FolderDetail
import com.telegramtv.data.model.FolderUpdateRequest
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class FoldersRepository @Inject constructor(
    private val api: TelePlayApi,
) {
    suspend fun getFolders(parentId: Int? = null): Result<List<Folder>> = runCatching { api.getFolders(parentId) }

    suspend fun getFolder(id: Int): Result<FolderDetail> = runCatching {
        val folder = api.getFolder(id)
        val subfolders = api.getFolders(parentId = id)
        val files = api.getFiles(folderId = id).items
        FolderDetail(folder = folder, subfolders = subfolders, files = files)
    }

    suspend fun createFolder(name: String, parentId: Int? = null): Result<Folder> =
        runCatching { api.createFolder(FolderCreateRequest(name = name, parentId = parentId)) }

    suspend fun updateFolder(id: Int, name: String? = null, parentId: Int? = null): Result<Folder> =
        runCatching { api.updateFolder(id, FolderUpdateRequest(name = name, parentId = parentId)) }

    suspend fun deleteFolder(id: Int): Result<Unit> = runCatching { api.deleteFolder(id); Unit }
}
