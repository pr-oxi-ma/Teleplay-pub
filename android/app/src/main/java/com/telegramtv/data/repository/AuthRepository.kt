package com.telegramtv.data.repository

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.telegramtv.data.api.TelePlayApi
import com.telegramtv.data.model.*
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import javax.inject.Inject
import javax.inject.Provider
import javax.inject.Singleton

private val Context.authDataStore by preferencesDataStore(name = "teleplay_auth")

@Singleton
class AuthRepository @Inject constructor(
    @ApplicationContext private val context: Context,
    private val apiProvider: Provider<TelePlayApi>,
) {
    private object Keys {
        val ACCESS_TOKEN = stringPreferencesKey("access_token")
        val REFRESH_TOKEN = stringPreferencesKey("refresh_token")
        val USER_NAME = stringPreferencesKey("user_name")
        val SESSION_TYPE = stringPreferencesKey("session_type")
    }

    private var heartbeatJob: Job? = null

    val isLoggedIn: Flow<Boolean> = context.authDataStore.data.map { prefs ->
        !prefs[Keys.ACCESS_TOKEN].isNullOrBlank()
    }

    val userName: Flow<String?> = context.authDataStore.data.map { prefs ->
        prefs[Keys.USER_NAME]
    }

    val sessionType: Flow<String?> = context.authDataStore.data.map { prefs ->
        prefs[Keys.SESSION_TYPE]
    }

    suspend fun getAccessToken(): String? = context.authDataStore.data.first()[Keys.ACCESS_TOKEN]
    suspend fun getRefreshToken(): String? = context.authDataStore.data.first()[Keys.REFRESH_TOKEN]

    suspend fun loginWithPassword(username: String, password: String): Result<AuthResponse> = runCatching {
        val response = apiProvider.get().loginWithPassword(
            PasswordLoginRequest(normalizeUsername(username), normalizePassword(password))
        )
        saveAuth(response, sessionType = "persistent")
        response
    }

    suspend fun generateLoginCode(): Result<LoginCodeResponse> = runCatching { apiProvider.get().generateLoginCode() }

    suspend fun pollLoginCode(code: String): Result<PollCodeResponse> = runCatching {
        val response = apiProvider.get().pollLoginCode(VerifyCodeRequest(code.trim().uppercase()))
        if (response.isClaimed) {
            saveAuth(
                AuthResponse(
                    accessToken = response.accessToken!!,
                    refreshToken = response.refreshToken!!,
                    expiresIn = response.expiresIn,
                    user = response.user,
                ),
                sessionType = "temporary",
            )
        }
        response
    }

    suspend fun verifyLoginCode(code: String): Result<AuthResponse> = runCatching {
        val response = apiProvider.get().verifyLoginCode(VerifyCodeRequest(code.trim().uppercase()))
        saveAuth(response, sessionType = "temporary")
        response
    }

    suspend fun getBotInfo(): Result<BotInfoResponse> = runCatching { apiProvider.get().getBotInfo() }

    suspend fun refreshTokenIfPossible(): Boolean {
        val refreshToken = getRefreshToken() ?: return false
        return try {
            val token = apiProvider.get().refresh(RefreshTokenRequest(refreshToken))
            context.authDataStore.edit { prefs ->
                prefs[Keys.ACCESS_TOKEN] = token.accessToken
                prefs[Keys.REFRESH_TOKEN] = token.refreshToken
            }
            true
        } catch (_: Throwable) {
            clearAuth()
            false
        }
    }

    suspend fun logout() {
        runCatching { apiProvider.get().logout() }
        clearAuth()
    }

    suspend fun logoutAll() {
        runCatching { apiProvider.get().logoutAll() }
        clearAuth()
    }

    suspend fun clearAuth() {
        heartbeatJob?.cancel()
        heartbeatJob = null
        context.authDataStore.edit { prefs -> prefs.clear() }
    }

    fun startHeartbeat(scope: CoroutineScope) {
        if (heartbeatJob?.isActive == true) return
        heartbeatJob = scope.launch {
            while (isActive) {
                delay(60_000)
                if (sessionType.first() == "temporary" && getAccessToken() != null) {
                    runCatching { apiProvider.get().heartbeat() }
                        .onFailure { refreshTokenIfPossible() }
                }
            }
        }
    }

    fun closeTemporarySession(scope: CoroutineScope) {
        scope.launch {
            if (sessionType.first() == "temporary") {
                runCatching { apiProvider.get().closeTemporarySession() }
                clearAuth()
            }
        }
    }

    private suspend fun saveAuth(response: AuthResponse, sessionType: String) {
        context.authDataStore.edit { prefs ->
            prefs[Keys.ACCESS_TOKEN] = response.accessToken
            prefs[Keys.REFRESH_TOKEN] = response.refreshToken
            prefs[Keys.SESSION_TYPE] = sessionType
            response.user?.let { user ->
                val display = user.username ?: user.firstName ?: "Telegram ${user.telegramId}"
                prefs[Keys.USER_NAME] = display
            }
        }
    }

    private fun normalizeUsername(value: String): String = value.lowercase().replace(Regex("\\s+"), "")
    private fun normalizePassword(value: String): String = value.replace(Regex("\\s+"), "")
}
