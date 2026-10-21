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
            // Pin AGP to the NDK the pinned CPython 3.13 Android build itself requires
            // (its android-env.sh hard-codes 27.3.13750724): without this AGP hunts its
            // own default revision, which a job-private SDK may not carry.
            ndkVersion = "27.3.13750724"
        }
    }

    compileOptions {
        // The Kotlin/Compose toolchain compiles at JVM 17 while AGP's javac default is
        // 1.8, and Gradle's target validation rejects that mix (observed on the fleet:
        // "Inconsistent JVM-target compatibility ... compileDebugJavaWithJavac (1.8)
        // and compileDebugKotlin (17)"). Align javac with Kotlin on the pinned JDK 17.
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
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
        // buildPython must match the embedded 3.13 in major.minor
        // (chaquo.com/chaquopy/doc/current/android.html#buildpython). The feasibility
        // runner provisions a task-private CPython 3.13 and passes it as -PbuildPython=...;
        // the host's own default python3 (3.14) must never be picked up implicitly.
        providers.gradleProperty("buildPython").orNull?.let { buildPython(it) }
        pip {
            // When the build network can only reach an allowlisted mirror, it arrives as
            // -PpipIndexUrl=... and replaces pip's index entirely: Chaquopy omits its own
            // chaquo.com repository exactly when a user --index-url is present
            // (pip_install.py in 17.1.0), and the pinned zstandard wheels still come from
            // --find-links below.
            providers.gradleProperty("pipIndexUrl").orNull?.let { options("--index-url", it) }
            // zstandard is a cp313 Android wheel, not a pure-Python requirement: Chaquopy
            // does not cross-build native sdsts. The wheels are built on the fleet from the
            // pinned 0.25.0 sdist with cibuildwheel (packaging/android/zstandard-wheel/),
            // and the wheel directory arrives here as -PzstandardWheels=... The exact
            // requirement plus --find-links means pip resolves to those wheels only; the
            // Chaquopy repository (which stops at 0.23) is never consulted and the floor
            // is never downgraded.
            install("httpx>=0.27,<1")
            // A build without the wheel directory fails here rather than silently shipping
            // an APK with no zstandard in it.
            val wheelDir = providers.gradleProperty("zstandardWheels")
            if (!wheelDir.isPresent) {
                throw GradleException(
                    "zstandardWheels must name the cibuildwheel cp313 Android wheel directory " +
                    "(see packaging/android/zstandard-wheel/RECIPE.md)")
            }
            options("--find-links", wheelDir.get())
            install("zstandard==0.25.0")
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

// Chaquopy's per-variant Python source merge consumes the directory syncCorePython
// writes; Gradle's task-dependency validation rejects that as an implicit dependency
// (observed: ":app:mergeDebugPythonSources uses this output of task ':app:syncCorePython'
// without declaring an explicit or implicit dependency"), so declare it on the merge
// tasks themselves.
tasks.matching { it.name.endsWith("PythonSources") }.configureEach {
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
