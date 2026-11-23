pluginManagement {
    buildscript {
        repositories {
            google()
            mavenCentral()
        }
        dependencies {
            // Kotlin 2.4 class files require R8 9.1.29 or newer.
            classpath("com.android.tools:r8:9.1.31")
        }
    }
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "comic-archiver"
include(":app")
