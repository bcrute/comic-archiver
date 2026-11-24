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

// The words both apps show, packed where the phone's own code reads them: one file, written once.
val syncWords = tasks.register<Sync>("syncWords") {
    from(rootProject.projectDir.resolve("../comic_archive/messages.json"))
    into(layout.buildDirectory.dir("generated/words"))
}

// The license texts the About page shows, packed as assets: Comic Archiver's own license, the
// third-party notices, the texts no package brings (Apache-2.0 for AndroidX, Compose and Kotlin;
// Chaquopy's; Python's and those of the libraries built into it) and each Python package's own.
// scripts/android_feasibility.py checks that a built APK carries exactly these.
val syncLicenses = tasks.register<Sync>("syncLicenses") {
    val project = rootProject.projectDir.resolve("..")
    // Named one by one: a pattern over the repository would walk this build's own output.
    from(project.resolve("LICENSE"), project.resolve("THIRD-PARTY-NOTICES.md"))
    from(project.resolve("packaging/licenses")) {
        include(listOf("Apache-2.0", "chaquopy", "python-3.13", "bzip2", "expat", "libffi", "liblzma",
            "mpdecimal", "openssl", "sqlite", "zlib").map { "$it.txt" })
        into("other")
    }
    from(project.resolve("packaging/licenses/android"))
    into(layout.buildDirectory.dir("generated/license-assets/licenses"))
}

android {
    namespace = "io.github.bcrute.comicarchiver"
    // Raising to 37 is pending fleet SDK availability; the dev phone at API 37 runs 36 targets.
    compileSdk = 36

    defaultConfig {
        applicationId = "io.github.bcrute.comicarchiver"
        minSdk = 29
        targetSdk = 36
        versionCode = 7
        versionName = "0.5.6"
        testInstrumentationRunner = "io.github.bcrute.comicarchiver.integration.BetaTestRunner"
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
        debug {
            // One managed debug signing identity for fleet builds: scripts/android_feasibility.py
            // materializes a job-private keystore (injected from owner-private config or freshly
            // generated on the job) and passes its path as -PdebugKeystore=... The alias and both
            // passwords are the public Android debug defaults, so only the store file changes.
            // Without the property AGP's default behavior (a per-user auto-generated debug
            // keystore, unique per job and lost with it) applies unchanged. The release key
            // never reaches a debug build.
            providers.gradleProperty("debugKeystore").orNull?.let { managed ->
                signingConfigs.getByName("debug") {
                    storeFile = file(managed)
                    storePassword = "android"
                    keyAlias = "androiddebugkey"
                    keyPassword = "android"
                }
            }
        }
        release {
            isMinifyEnabled = false
        }
    }

    buildFeatures {
        compose = true
    }

    sourceSets {
        getByName("main") {
            assets.srcDir(layout.buildDirectory.dir("generated/license-assets"))
            resources.srcDir(layout.buildDirectory.dir("generated/words"))
        }
    }

    // Render and interact with Compose on the CI fleet without requiring KVM. These
    // screen tests complement (and do not replace) device/WebView/SAF qualification.
    testOptions {
        unitTests.isIncludeAndroidResources = true
        unitTests.all {
            // Robolectric's resolver puts its download lock in Java's user.home.
            // Fleet host homes are read-only: only this test JVM uses a private home,
            // leaving the environment and host/shared caches untouched.
            val testHome = layout.buildDirectory.dir("test-home/${it.name}").get().asFile
            it.systemProperty("user.home", testHome.absolutePath)
            it.doFirst { testHome.mkdirs() }
            it.jvmArgs(
                "--add-opens=java.base/java.lang=ALL-UNNAMED",
                "--add-opens=java.base/java.util=ALL-UNNAMED",
                "--add-opens=java.base/java.io=ALL-UNNAMED",
                "--add-opens=java.base/java.net=ALL-UNNAMED",
                "--add-opens=java.base/java.security=ALL-UNNAMED",
                "--add-opens=java.base/java.text=ALL-UNNAMED",
                "--add-opens=java.base/jdk.internal.access=ALL-UNNAMED",
                "--add-opens=java.desktop/java.awt.font=ALL-UNNAMED",
                "--add-opens=jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED",
            )
        }
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
            val lockedWheels = providers.gradleProperty("lockedPythonWheels").orNull
            if (lockedWheels != null) {
                options("--no-index", "--no-cache-dir", "--find-links", lockedWheels)
                install("-r", file("$lockedWheels/requirements.txt").absolutePath)
            } else {
                // Development builds use the configured index and the retained cp313 Android wheels.
                // Chaquopy omits its own repository when a custom --index-url is present.
                providers.gradleProperty("pipIndexUrl").orNull?.let { options("--index-url", it) }
                install("httpx>=0.27,<1")
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
    }
    sourceSets {
        getByName("main") {
            srcDir(layout.buildDirectory.dir("generated/core-python"))
        }
    }
}

tasks.named("preBuild") {
    dependsOn(syncCorePython, syncLicenses, syncWords)
}

// The tasks that pack the words, for the app and for its tests.
tasks.matching { it.name.contains("JavaRes") }.configureEach {
    dependsOn(syncWords)
}

// The same for the tasks that read the assets, as for the Python sources below.
tasks.matching { it.name.endsWith("Assets") }.configureEach {
    dependsOn(syncLicenses)
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
    // AGP resolves lint in a detached configuration. The constraint belongs to the
    // lint tool's metadata so it follows that configuration without changing app dependencies.
    val buildToolPins = rootProject.file("gradle/build-tool-pins.txt").readLines()
    components {
        withModule("com.android.tools.lint:lint-gradle") {
            allVariants {
                withDependencyConstraints {
                    buildToolPins.forEach { pin ->
                        add(pin) {
                            version { strictly(pin.substringAfterLast(':')) }
                        }
                    }
                }
            }
        }
    }
    implementation(libs.androidx.webkit)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.core)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.icons)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.kotlinx.coroutines.android)
    // JVM unit tests for the UI/export line's exporter and models; org.json stands in for
    // the platform's copy that only exists on a device (UI line's test-only ask).
    testImplementation(libs.junit)
    testImplementation("org.json:json:20240303")
    testImplementation("org.robolectric:robolectric:4.17")
    testImplementation("androidx.compose.ui:ui-test-junit4")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
    // Disposable-emulator integration only; these classes/assets never ship in app-debug.apk.
    androidTestImplementation(libs.junit)
    androidTestImplementation("androidx.test:runner:1.7.0")
    androidTestImplementation("androidx.test:core:1.7.0")
    androidTestImplementation("androidx.test.ext:junit:1.3.0")
    androidTestImplementation("androidx.test.uiautomator:uiautomator:2.3.0")
}
