package com.telegramtv.data.api

import com.telegramtv.data.repository.AuthRepository
import kotlinx.coroutines.runBlocking
import okhttp3.Interceptor
import okhttp3.Response
import javax.inject.Inject
import javax.inject.Provider
import javax.inject.Singleton

@Singleton
class AuthInterceptor @Inject constructor(
    private val authRepositoryProvider: Provider<AuthRepository>,
) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val original = chain.request()
        val token = runBlocking { authRepositoryProvider.get().getAccessToken() }
        val request = if (!token.isNullOrBlank()) {
            original.newBuilder().header("Authorization", "Bearer $token").build()
        } else {
            original
        }

        val response = chain.proceed(request)
        if (response.code != 401 || original.header("X-TelePlay-Retry") == "1") {
            return response
        }

        response.close()
        val refreshed = runBlocking { authRepositoryProvider.get().refreshTokenIfPossible() }
        if (!refreshed) return chain.proceed(original)

        val newToken = runBlocking { authRepositoryProvider.get().getAccessToken() }
        val retry = original.newBuilder()
            .header("X-TelePlay-Retry", "1")
            .apply { if (!newToken.isNullOrBlank()) header("Authorization", "Bearer $newToken") }
            .build()
        return chain.proceed(retry)
    }
}
