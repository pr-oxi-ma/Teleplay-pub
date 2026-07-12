package com.telegramtv.data.model

import com.google.gson.annotations.SerializedName

/** User/account models shared by Android TV and mobile UI. */
data class User(
    val id: Int,
    @SerializedName("telegram_id") val telegramId: Long,
    val username: String? = null,
    @SerializedName("first_name") val firstName: String? = null,
    @SerializedName("last_name") val lastName: String? = null,
    @SerializedName("created_at") val createdAt: String? = null,
    @SerializedName("last_active") val lastActive: String? = null,
)

data class AuthResponse(
    @SerializedName("access_token") val accessToken: String,
    @SerializedName("refresh_token") val refreshToken: String,
    @SerializedName("token_type") val tokenType: String = "bearer",
    @SerializedName("expires_in") val expiresIn: Int? = null,
    val user: User? = null,
)

data class TokenResponse(
    @SerializedName("access_token") val accessToken: String,
    @SerializedName("refresh_token") val refreshToken: String,
    @SerializedName("token_type") val tokenType: String = "bearer",
    @SerializedName("expires_in") val expiresIn: Int? = null,
)

data class PasswordLoginRequest(
    val username: String,
    val password: String,
)

data class RefreshTokenRequest(
    @SerializedName("refreshToken") val refreshToken: String,
)

data class LoginCodeResponse(
    val code: String,
    @SerializedName("expires_at") val expiresAt: String,
)

data class VerifyCodeRequest(val code: String)

data class PollCodeResponse(
    val status: String,
    val message: String? = null,
    @SerializedName("access_token") val accessToken: String? = null,
    @SerializedName("refresh_token") val refreshToken: String? = null,
    @SerializedName("token_type") val tokenType: String = "bearer",
    @SerializedName("expires_in") val expiresIn: Int? = null,
    val user: User? = null,
) {
    val isClaimed: Boolean get() = status == "claimed" && !accessToken.isNullOrBlank() && !refreshToken.isNullOrBlank()
}

data class BotInfoResponse(
    val username: String,
    val name: String? = null,
    @SerializedName("server_version") val serverVersion: String? = null,
)

data class MessageResponse(val message: String? = null)

/** File/folder models. Backend returns snake_case, UI uses camelCase. */
data class FileItem(
    val id: Int,
    @SerializedName("file_name") val fileName: String,
    @SerializedName("file_size") val fileSize: Long = 0L,
    @SerializedName("mime_type") val mimeType: String? = null,
    @SerializedName("file_type") val fileType: String = "document",
    val duration: Double? = null,
    val width: Int? = null,
    val height: Int? = null,
    @SerializedName("folder_id") val folderId: Int? = null,
    @SerializedName("thumbnail_url") val thumbnailUrl: String? = null,
    @SerializedName("stream_url") val streamUrl: String? = null,
    @SerializedName("fallback_stream_url") val fallbackStreamUrl: String? = null,
    @SerializedName("download_url") val downloadUrl: String? = null,
    @SerializedName("public_hash") val publicHash: String? = null,
    @SerializedName("public_stream_url") val publicStreamUrl: String? = null,
    @SerializedName("last_pos") val lastPos: Int = 0,
) {
    val isVideo: Boolean get() = fileType.equals("video", ignoreCase = true)
    val isAudio: Boolean get() = fileType.equals("audio", ignoreCase = true)
    val isImage: Boolean get() = fileType.equals("image", ignoreCase = true)
    val isTimedMedia: Boolean get() = isVideo || isAudio
}

data class FileListResponse(
    @SerializedName("files") val items: List<FileItem> = emptyList(),
    val total: Int = 0,
    val page: Int = 1,
    @SerializedName("per_page") val perPage: Int = 50,
)

data class Folder(
    val id: Int,
    val name: String,
    @SerializedName("parent_id") val parentId: Int? = null,
    @SerializedName("file_count") val fileCount: Int? = null,
    val children: List<Folder> = emptyList(),
)

data class FolderDetail(
    val folder: Folder,
    val subfolders: List<Folder> = emptyList(),
    val files: List<FileItem> = emptyList(),
    val parentPath: List<Folder> = emptyList(),
)

data class TVBrowseResponse(
    val continueWatching: List<FileItem> = emptyList(),
    val recentFiles: List<FileItem> = emptyList(),
    val folders: List<Folder> = emptyList(),
)

data class WatchProgress(
    val id: Int? = null,
    @SerializedName("file_id") val fileId: Int? = null,
    val position: Int = 0,
    val duration: Double? = null,
    val completed: Boolean = false,
    @SerializedName("updated_at") val updatedAt: String? = null,
)

data class WatchProgressUpdate(
    val position: Int,
    val duration: Double? = null,
    val completed: Boolean? = null,
)

data class FileUpdateRequest(
    @SerializedName("file_name") val fileName: String? = null,
    @SerializedName("folder_id") val folderId: Int? = null,
)

data class FolderCreateRequest(
    val name: String,
    @SerializedName("parent_id") val parentId: Int? = null,
)

data class FolderUpdateRequest(
    val name: String? = null,
    @SerializedName("parent_id") val parentId: Int? = null,
)
