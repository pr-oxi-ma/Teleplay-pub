import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.google.dagger.hilt.android")
    kotlin("kapt")
}

android {
    namespace = "com.telegramtv"
    compileSdk = 35

    // Load local.properties at android block level.
    // Env variables win, local.properties is second, code fallback is last for testing.
    val localProperties = Properties()
    val localPropertiesFile = rootProject.file("local.properties")
    if (localPropertiesFile.exists()) {
        localProperties.load(localPropertiesFile.inputStream())
    }

    fun configValue(name: String, fallback: String = ""): String {
        return System.getenv(name)
            ?: localProperties.getProperty(name)
            ?: fallback
    }

    fun quotedBuildConfigString(value: String): String {
        val escaped = value.replace("\\", "\\\\").replace("\"", "\\\"")
        return "\"$escaped\""
    }

    defaultConfig {
        applicationId = "com.telegramtv"
        minSdk = 21
        targetSdk = 35
        versionCode = 6
        versionName = "1.0.1"

        val serverUrl = configValue(
            "TELEPLAY_ANDROID_SERVER_URL",
            configValue("TELEGRAM_TV_SERVER_URL", "http://10.0.2.2:8000")
        )
        val botUsername = configValue("TELEPLAY_ANDROID_BOT_USERNAME", "TelegramTV_Bot")
        buildConfigField("String", "DEFAULT_SERVER_URL", quotedBuildConfigString(serverUrl))
        buildConfigField("String", "DEFAULT_BOT_USERNAME", quotedBuildConfigString(botUsername))
    }

    signingConfigs {
        create("release") {
            // Prioritize environment variables for CI/CD, fallback to local.properties
            val storeFilePath = configValue("RELEASE_STORE_FILE", "../my-release-key.jks")
            storeFile = file(storeFilePath)
            storePassword = configValue("RELEASE_STORE_PASSWORD")
            keyAlias = configValue("RELEASE_KEY_ALIAS", "my-key-alias")
            keyPassword = configValue("RELEASE_KEY_PASSWORD")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            signingConfig = signingConfigs.getByName("release")
        }
        debug {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.15"
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }

    // Split APKs by ABI to reduce size (FFmpeg adds ~50MB per architecture)
    splits {
        abi {
            isEnable = true
            reset()
            include("armeabi-v7a", "arm64-v8a", "x86", "x86_64")
            isUniversalApk = true
        }
    }
}

dependencies {
    // Kotlin
    implementation("org.jetbrains.kotlin:kotlin-stdlib:1.9.25")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")

    // AndroidX Core
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")

    // Compose
    implementation(platform("androidx.compose:compose-bom:2024.12.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.compose.animation:animation")
    implementation("androidx.compose.animation:animation-core")
    debugImplementation("androidx.compose.ui:ui-tooling")
    debugImplementation("androidx.compose.ui:ui-test-manifest")

    // Compose for TV
    implementation("androidx.tv:tv-foundation:1.0.0-alpha10")
    implementation("androidx.tv:tv-material:1.0.0-alpha10")

    // Navigation
    implementation("androidx.navigation:navigation-compose:2.8.5")

    // Media3 (ExoPlayer) - Core
    implementation("androidx.media3:media3-exoplayer:1.10.1")
    implementation("androidx.media3:media3-ui:1.10.1")
    implementation("androidx.media3:media3-session:1.10.1")
    implementation("androidx.media3:media3-common:1.10.1")

    // Media3 - Format support
    implementation("androidx.media3:media3-exoplayer-dash:1.10.1")
    implementation("androidx.media3:media3-exoplayer-hls:1.10.1")
    implementation("androidx.media3:media3-datasource-okhttp:1.10.1")

    // Note: Standard ExoPlayer supports HEVC, VP9, Opus, AAC, and most common formats
    // For DTS/AC3 software decoding, add FFmpeg extension manually if needed

    // Networking
    implementation("com.squareup.retrofit2:retrofit:2.11.0")
    implementation("com.squareup.retrofit2:converter-gson:2.11.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.squareup.okhttp3:logging-interceptor:4.12.0")
    implementation("com.google.code.gson:gson:2.11.0")

    // Dependency Injection - Hilt
    implementation("com.google.dagger:hilt-android:2.52")
    kapt("com.google.dagger:hilt-compiler:2.52")
    implementation("androidx.hilt:hilt-navigation-compose:1.2.0")

    // Image Loading - Coil
    implementation("io.coil-kt:coil-compose:2.7.0")

    // Security - Encrypted Preferences
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    // Leanback (for TV-specific components not in Compose TV yet)
    implementation("androidx.leanback:leanback:1.0.0")

    // Datastore for preferences
    implementation("androidx.datastore:datastore-preferences:1.1.1")

    // Testing
    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.6.1")
}

kapt {
    correctErrorTypes = true
}
