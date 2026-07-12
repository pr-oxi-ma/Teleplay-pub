package com.telegramtv.ui.auth

import android.content.Context
import android.content.Intent
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.telegramtv.data.repository.AuthRepository
import com.telegramtv.data.repository.SettingsRepository
import dagger.hilt.android.lifecycle.HiltViewModel
import dagger.hilt.android.qualifiers.ApplicationContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import javax.inject.Inject

/**
 * Login screen state.
 */
data class LoginUiState(
    val loginCode: String? = null,
    val expiresAt: String? = null,
    val isLoading: Boolean = true,
    val isPolling: Boolean = false,
    val isLoggedIn: Boolean = false,
    val error: String? = null,
    val debugLog: String = "",
    val serverUrl: String = "",
    val botUsername: String = "",
    val showServerConfig: Boolean = false,
    val loginMode: LoginMode = LoginMode.PASSWORD,
    val username: String = "",
    val password: String = "",
    val passwordLoginLoading: Boolean = false,
    val passwordLoginError: String? = null
)

enum class LoginMode { PASSWORD, CODE }

/**
 * ViewModel for the login screen.
 */
@HiltViewModel
class LoginViewModel @Inject constructor(
    @ApplicationContext private val context: Context,
    private val authRepository: AuthRepository,
    private val settingsRepository: SettingsRepository
) : ViewModel() {

    private val _uiState = MutableStateFlow(LoginUiState())
    val uiState: StateFlow<LoginUiState> = _uiState.asStateFlow()

    private var pollingJob: kotlinx.coroutines.Job? = null

    init {
        loadServerUrl()
    }

    /**
     * Load saved server URL.
     */
    private fun loadServerUrl() {
        viewModelScope.launch {
            val url = settingsRepository.serverUrl.first()
            val bot = settingsRepository.botUsername.first()
            _uiState.value = _uiState.value.copy(serverUrl = url, botUsername = bot)
            
            if (url.isNotEmpty()) {
                fetchBotInfo()
                _uiState.value = _uiState.value.copy(isLoading = false)
            }
        }
    }

    /**
     * Update server URL and save it.
     */
    fun updateServerUrl(url: String) {
        _uiState.value = _uiState.value.copy(serverUrl = url)
        
        // Auto-fetch bot info if it looks like a valid URL
        if (url.startsWith("http") && url.length > 10) {
            fetchBotInfo()
        }
    }

    /**
     * Fetch bot info from the backend.
     */
    fun fetchBotInfo() {
        viewModelScope.launch {
            authRepository.getBotInfo().onSuccess { botInfo ->
                _uiState.value = _uiState.value.copy(botUsername = botInfo.username)
                settingsRepository.setBotUsername(botInfo.username)
            }
        }
    }

    /**
     * Update bot username and save it.
     */
    fun updateBotUsername(username: String) {
        _uiState.value = _uiState.value.copy(botUsername = username)
        viewModelScope.launch {
            settingsRepository.setBotUsername(username)
        }
    }

    /**
     * Toggle server config visibility.
     */
    fun toggleServerConfig() {
        _uiState.value = _uiState.value.copy(
            showServerConfig = !_uiState.value.showServerConfig
        )
    }

    fun switchLoginMode(mode: LoginMode) {
        stopPolling()
        _uiState.value = _uiState.value.copy(
            loginMode = mode,
            error = null,
            passwordLoginError = null,
            isLoading = false
        )
        if (mode == LoginMode.CODE && _uiState.value.loginCode == null) {
            generateLoginCode()
        }
    }

    fun updateUsername(value: String) {
        _uiState.value = _uiState.value.copy(username = value.lowercase().replace(Regex("\\s+"), ""), passwordLoginError = null)
    }

    fun updatePassword(value: String) {
        _uiState.value = _uiState.value.copy(password = value.replace(Regex("\\s+"), ""), passwordLoginError = null)
    }

    fun loginWithPassword() {
        val username = _uiState.value.username
        val password = _uiState.value.password
        if (username.isBlank() || password.isBlank()) {
            _uiState.value = _uiState.value.copy(passwordLoginError = "Enter username and password")
            return
        }

        viewModelScope.launch {
            settingsRepository.setServerUrl(_uiState.value.serverUrl)
            _uiState.value = _uiState.value.copy(passwordLoginLoading = true, passwordLoginError = null)
            authRepository.loginWithPassword(username, password).fold(
                onSuccess = { _ ->
                    _uiState.value = _uiState.value.copy(passwordLoginLoading = false, isLoggedIn = true)
                },
                onFailure = { e ->
                    _uiState.value = _uiState.value.copy(
                        passwordLoginLoading = false,
                        passwordLoginError = e.message?.takeIf { it.isNotBlank() } ?: "Invalid credentials"
                    )
                }
            )
        }
    }

    /**
     * Save server URL and restart the app.
     */
    fun saveAndRestart() {
        viewModelScope.launch {
            val url = _uiState.value.serverUrl
            if (url.isNotEmpty()) {
                settingsRepository.setServerUrl(url)
                val intent = context.packageManager.getLaunchIntentForPackage(context.packageName)
                intent?.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_NEW_TASK)
                context.startActivity(intent)
                Runtime.getRuntime().exit(0)
            }
        }
    }

    /**
     * Generate a new login code.
     */
    fun generateLoginCode() {
        // Stop any existing polling for an old code
        stopPolling()

        viewModelScope.launch {
            // Save server URL before generating code
            settingsRepository.setServerUrl(_uiState.value.serverUrl)

            _uiState.value = _uiState.value.copy(
                isLoading = true, 
                error = null,
                debugLog = "Starting generateLoginCode...\n"
            )

            try {
                val result = authRepository.generateLoginCode()
                
                result.fold(
                    onSuccess = { response ->
                        _uiState.value = _uiState.value.copy(
                            loginCode = response.code,
                            expiresAt = response.expiresAt,
                            isLoading = false,
                            debugLog = _uiState.value.debugLog + "Success! Code: ${response.code}\n"
                        )
                        startPolling(response.code)
                    },
                    onFailure = { e ->
                        e.printStackTrace()
                        _uiState.value = _uiState.value.copy(
                            isLoading = false,
                            error = "Failed: ${e.message}",
                            debugLog = _uiState.value.debugLog + "Failed: ${e.message}\n"
                        )
                    }
                )
            } catch (e: Exception) {
                 _uiState.value = _uiState.value.copy(
                    isLoading = false,
                    error = "Crash: ${e.message}",
                    debugLog = _uiState.value.debugLog + "Crash: ${e.message}\n"
                )
            }
        }
    }

    /**
     * Start polling for login confirmation.
     */
    private fun startPolling(code: String) {
        stopPolling()

        pollingJob = viewModelScope.launch {
            _uiState.value = _uiState.value.copy(isPolling = true)

            // Poll every 3 seconds for 1 minute, then every 6 seconds up to 5 minutes.
            repeat(80) { attempt ->
                val result = authRepository.pollLoginCode(code)
                result.fold(
                    onSuccess = { response ->
                        if (response.isClaimed) {
                            _uiState.value = _uiState.value.copy(
                                isPolling = false,
                                isLoggedIn = true
                            )
                            return@launch
                        }
                    },
                    onFailure = { e ->
                        if (e.message?.contains("expired", ignoreCase = true) == true ||
                            e.message?.contains("invalid", ignoreCase = true) == true) {
                            _uiState.value = _uiState.value.copy(
                                isPolling = false,
                                error = "Code expired. Please generate a new one."
                            )
                            return@launch
                        }
                    }
                )

                delay(if (attempt < 20) 3000 else 6000)
            }

            // Timeout after 5 minutes
            _uiState.value = _uiState.value.copy(
                isPolling = false,
                error = "Login timeout. Please try again."
            )
        }
    }

    /**
     * Stop polling.
     */
    fun stopPolling() {
        pollingJob?.cancel()
        pollingJob = null
        _uiState.value = _uiState.value.copy(isPolling = false)
    }

    override fun onCleared() {
        super.onCleared()
        stopPolling()
    }
}

