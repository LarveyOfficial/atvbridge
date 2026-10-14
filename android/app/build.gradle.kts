import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.larvey.atvbridge"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.larvey.atvbridge"
        minSdk = 29
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }

    // Release signing: CI passes the keystore via ATVB_KEYSTORE* env vars (see
    // .github/workflows/build.yml); locally ~/.android/atvbridge-release.properties
    // (storeFile, storePassword, keyAlias, keyPassword). Without either: the debug key.
    val signing: Map<String, String>? = System.getenv("ATVB_KEYSTORE")?.takeIf { it.isNotBlank() }?.let {
        mapOf(
            "storeFile" to it,
            "storePassword" to System.getenv("ATVB_KEYSTORE_PASSWORD").orEmpty(),
            "keyAlias" to System.getenv("ATVB_KEY_ALIAS").orEmpty(),
            "keyPassword" to System.getenv("ATVB_KEY_PASSWORD").orEmpty(),
        )
    } ?: File(System.getProperty("user.home"), ".android/atvbridge-release.properties")
        .takeIf { it.isFile }
        ?.let { f -> Properties().apply { f.inputStream().use { load(it) } } }
        ?.let { p -> p.stringPropertyNames().associateWith { p.getProperty(it) } }

    signingConfigs {
        if (signing != null) {
            create("release") {
                storeFile = file(signing.getValue("storeFile"))
                storePassword = signing["storePassword"]
                keyAlias = signing["keyAlias"]
                keyPassword = signing["keyPassword"]
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName(if (signing != null) "release" else "debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    testImplementation("junit:junit:4.13.2")
    // Real org.json for JVM tests (android.jar only has stubs).
    testImplementation("org.json:json:20240303")
}
