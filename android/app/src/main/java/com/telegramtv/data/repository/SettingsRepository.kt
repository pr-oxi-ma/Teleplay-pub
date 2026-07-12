package com.telegramtv.data.repository

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import com.telegramtv.BuildConfig
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import javax.inject.Inject
import javax.inject.Singleton

private val Context.settingsDataStore by preferencesDataStore(name = "teleplay_settings")

@Singleton
class SettingsRepository @Inject constructor(
    @ApplicationContext private val context: Context,
) {
    private object Keys {
        val SERVER_URL = stringPreferencesKey("server_url")
        val BOT_USERNAME = stringPreferencesKey("bot_username")
        val AUTO_PLAY_NEXT = booleanPreferencesKey("auto_play_next")
        val PREFERRED_QUALITY = stringPreferencesKey("preferred_quality")
    }

    val serverUrl: Flow<String> = context.settingsDataStore.data.map { prefs ->
        prefs[Keys.SERVER_URL] ?: BuildConfig.DEFAULT_SERVER_URL
    }

    val botUsername: Flow<String> = context.settingsDataStore.data.map { prefs ->
        prefs[Keys.BOT_USERNAME] ?: BuildConfig.DEFAULT_BOT_USERNAME
    }

    val autoPlayNext: Flow<Boolean> = context.settingsDataStore.data.map { prefs ->
        prefs[Keys.AUTO_PLAY_NEXT] ?: true
    }

    val preferredQuality: Flow<String> = context.settingsDataStore.data.map { prefs ->
        prefs[Keys.PREFERRED_QUALITY] ?: "auto"
    }

    suspend fun getServerUrl(): String = normalizeServerUrl(serverUrl.first())

    suspend fun setServerUrl(url: String) {
        context.settingsDataStore.edit { prefs -> prefs[Keys.SERVER_URL] = normalizeServerUrl(url) }
    }

    suspend fun setBotUsername(username: String) {
        context.settingsDataStore.edit { prefs -> prefs[Keys.BOT_USERNAME] = username.trim().removePrefix("@") }
    }

    suspend fun setAutoPlayNext(value: Boolean) {
        context.settingsDataStore.edit { prefs -> prefs[Keys.AUTO_PLAY_NEXT] = value }
    }

    suspend fun setPreferredQuality(value: String) {
        context.settingsDataStore.edit { prefs -> prefs[Keys.PREFERRED_QUALITY] = value }
    }

    private fun normalizeServerUrl(url: String): String {
        val cleaned = url.trim().trimEnd('/')
        return cleaned.ifBlank { BuildConfig.DEFAULT_SERVER_URL.trimEnd('/') }
    }
}
