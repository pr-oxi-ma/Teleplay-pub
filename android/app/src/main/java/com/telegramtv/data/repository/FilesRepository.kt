package com.telegramtv.data.repository

import com.telegramtv.data.api.TelePlayApi
import com.telegramtv.data.model.*
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class FilesRepository @Inject constructor(
    private val api: TelePlayApi,
) {
    suspend fun getTVBrowse(): Result<TVBrowseResponse> = Result.failure(
        UnsupportedOperationException("Combined TV browse endpoint is not available; use fallback calls")
    )

    suspend fun getFiles(folderId: Int? = null, page: Int = 1, perPage: Int = 50): Result<FileListResponse> =
        runCatching { api.getFiles(folderId = folderId, page = page, perPage = perPage) }

    suspend fun searchFiles(query: String, limit: Int = 50): Result<List<FileItem>> = runCatching {
        api.getFiles(search = query, page = 1, perPage = limit).items
    }

    suspend fun getRecentFiles(limit: Int = 50): Result<List<FileItem>> =
        runCatching { api.getRecentFiles(limit).items }

    suspend fun getContinueWatching(limit: Int = 50): Result<List<FileItem>> =
        runCatching { api.getContinueWatching(limit).items }

    suspend fun getFile(id: Int): Result<FileItem> = runCatching { api.getFile(id) }

    suspend fun deleteFile(id: Int): Result<Unit> = runCatching { api.deleteFile(id); Unit }

    suspend fun updateFile(id: Int, name: String? = null, folderId: Int? = null): Result<FileItem> =
        runCatching { api.updateFile(id, FileUpdateRequest(fileName = name, folderId = folderId)) }

    suspend fun getWatchProgress(fileId: Int): Result<WatchProgress?> = runCatching { api.getProgress(fileId) }

    suspend fun updateWatchProgress(
        fileId: Int,
        position: Int,
        duration: Int? = null,
        completed: Boolean = false,
    ): Result<Unit> = runCatching {
        api.updateProgress(
            fileId,
            WatchProgressUpdate(
                position = position,
                duration = duration?.toDouble(),
                completed = completed,
            )
        )
        Unit
    }

    suspend fun getPublicLink(fileId: Int, serverUrl: String): Result<String> = runCatching {
        val file = api.createShare(fileId)
        file.publicStreamUrl?.let { path ->
            if (path.startsWith("http")) path else serverUrl.trimEnd('/') + path
        } ?: "$serverUrl/api/stream/$fileId"
    }

    suspend fun revokeShare(fileId: Int): Result<FileItem> = runCatching { api.revokeShare(fileId) }
}
