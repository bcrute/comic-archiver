buildscript {
    val buildToolPins = rootDir.resolve("gradle/build-tool-pins.txt").readLines()
    // AGP and Chaquopy share this plugin classpath. Keep their tool dependencies aligned.
    configurations.named("classpath") {
        resolutionStrategy.force(*buildToolPins.toTypedArray())
    }
}

plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.kotlin.compose) apply false
    alias(libs.plugins.chaquopy) apply false
}
