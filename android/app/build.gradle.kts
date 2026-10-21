plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.chaquopy)
}

// The repository's Python core is synced into the build untouched; the app never edits it.
val syncCorePython = tasks.register<Sync>("syncCorePython") {
    from(rootProject.projectDir.resolve("../comic_archive")) {
        exclude("__pycache__/**")
    }
    into(layout.buildDirectory.dir("generated/core-python/comic_archive"))
}

android {
    namespace = "io.github.bcrute.comicarchiver"
    // Raising to 37 is pending fleet SDK availability; the dev phone at API 37 runs 36 targets.
    compileSdk = 36

    defaultConfig {
        applicationId = "io.github.bcrute.comicarchiver"
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "0.3.0-beta1"
        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildTypes {
        release {
            // Release hygiene (signing, shrinking) is native-line work after the beta scope
            // is proven; no signing keys are created before the release owner's procedure.
            isMinifyEnabled = false
        }
    }

    buildFeatures {
        compose = true
    }
}

chaquopy {
    defaultConfig {
        version = "3.13"
        pip {
            // Pure-Python dependencies only. zstandard stays >=0.25,<1: the pinned sdist
            // (packaging/android/zstandard-wheel/, digest in sdist.sha256) is fetched and
            // verified by scripts/android_feasibility.py, passed here as an absolute path
            // through -PzstandardSdist=..., and built for Android by Chaquopy's toolchain.
            // Never installed from the Chaquopy repository (it stops at 0.23) and never
            // downgraded.
            install("httpx>=0.27,<1")
            providers.gradleProperty("zstandardSdist").orNull?.let { install(java.io.File(it)) }
        }
    }
    sourceSets {
        getByName("main") {
            srcDir(layout.buildDirectory.dir("generated/core-python"))
        }
    }
}

tasks.named("preBuild") {
    dependsOn(syncCorePython)
}

dependencies {
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.kotlinx.coroutines.android)
    // JVM unit tests for the UI/export line's exporter and models.
    testImplementation(libs.junit)
}
