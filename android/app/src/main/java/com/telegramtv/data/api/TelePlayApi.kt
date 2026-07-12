package com.telegramtv.data.api

import com.telegramtv.data.model.*
import retrofit2.Response
import retrofit2.http.*

interface TelePlayApi {
    @GET("auth/bot/info")
    suspend fun getBotInfo(): BotInfoResponse

    @POST("auth/password/login")
    suspend fun loginWithPassword(@Body body: PasswordLoginRequest): AuthResponse

    @POST("auth/code/generate")
    suspend fun generateLoginCode(): LoginCodeResponse

    @POST("auth/code/poll")
    suspend fun pollLoginCode(@Body body: VerifyCodeRequest): PollCodeResponse

    @POST("auth/code/verify")
    suspend fun verifyLoginCode(@Body body: VerifyCodeRequest): AuthResponse

    @POST("auth/refresh")
    suspend fun refresh(@Body body: RefreshTokenRequest): TokenResponse

    @GET("auth/me")
    suspend fun me(): User

    @POST("auth/logout")
    suspend fun logout(): MessageResponse

    @POST("auth/logout-all")
    suspend fun logoutAll(): MessageResponse

    @POST("auth/session/heartbeat")
    suspend fun heartbeat(): MessageResponse

    @POST("auth/session/close")
    suspend fun closeTemporarySession(): MessageResponse

    @GET("files")
    suspend fun getFiles(
        @Query("folder_id") folderId: Int? = null,
        @Query("file_type") fileType: String? = null,
        @Query("search") search: String? = null,
        @Query("page") page: Int = 1,
        @Query("per_page") perPage: Int = 50,
    ): FileListResponse

    @GET("files/recent")
    suspend fun getRecentFiles(@Query("limit") limit: Int = 50): FileListResponse

    @GET("files/continue-watching")
    suspend fun getContinueWatching(@Query("limit") limit: Int = 50): FileListResponse

    @GET("files/storage")
    suspend fun getStorage(): Map<String, Any>

    @GET("files/{id}")
    suspend fun getFile(@Path("id") id: Int): FileItem

    @PATCH("files/{id}")
    suspend fun updateFile(@Path("id") id: Int, @Body body: FileUpdateRequest): FileItem

    @DELETE("files/{id}")
    suspend fun deleteFile(@Path("id") id: Int): MessageResponse

    @POST("files/{id}/progress")
    suspend fun updateProgress(@Path("id") id: Int, @Body body: WatchProgressUpdate): MessageResponse

    @GET("files/{id}/progress")
    suspend fun getProgress(@Path("id") id: Int): WatchProgress?

    @POST("files/{id}/share")
    suspend fun createShare(@Path("id") id: Int): FileItem

    @DELETE("files/{id}/share")
    suspend fun revokeShare(@Path("id") id: Int): FileItem

    @GET("folders")
    suspend fun getFolders(@Query("parent_id") parentId: Int? = null): List<Folder>

    @GET("folders/tree")
    suspend fun getFolderTree(): List<Folder>

    @GET("folders/{id}")
    suspend fun getFolder(@Path("id") id: Int): Folder

    @POST("folders")
    suspend fun createFolder(@Body body: FolderCreateRequest): Folder

    @PATCH("folders/{id}")
    suspend fun updateFolder(@Path("id") id: Int, @Body body: FolderUpdateRequest): Folder

    @DELETE("folders/{id}")
    suspend fun deleteFolder(@Path("id") id: Int): MessageResponse
}
