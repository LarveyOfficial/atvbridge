plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.atvbridge.companion"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.atvbridge.companion"
        minSdk = 29
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }

    // CI signs with the keystore from ATVB_KEYSTORE (see .github/workflows/android.yml);
    // without it, release builds use the local debug key (fine for a sideloaded app).
    val keystore = System.getenv("ATVB_KEYSTORE")?.takeIf { it.isNotBlank() }
    signingConfigs {
        if (keystore != null) {
            create("release") {
                storeFile = file(keystore)
                storePassword = System.getenv("ATVB_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("ATVB_KEY_ALIAS")
                keyPassword = System.getenv("ATVB_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName(if (keystore != null) "release" else "debug")
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
