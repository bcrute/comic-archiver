package io.github.bcrute.comicarchiver.integration

import android.app.Application
import android.content.Context
import android.os.Build
import android.os.Bundle
import androidx.test.runner.AndroidJUnitRunner

/** Fail before fixture initialization on any physical phone or ordinary app launch. */
object FixtureSafety {
    lateinit var runToken: String
    fun requireEmulator() {
        check(Build.HARDWARE in setOf("ranchu", "goldfish")) {
            "Integration fixtures require a disposable Android emulator."
        }
    }
}

class BetaTestRunner : AndroidJUnitRunner() {
    override fun newApplication(cl: ClassLoader?, name: String?, context: Context?): Application {
        FixtureSafety.requireEmulator()
        return super.newApplication(cl, BetaTestApplication::class.java.name, context)
    }

    override fun onCreate(arguments: Bundle) {
        FixtureSafety.requireEmulator()
        check(arguments.getString("disposable_emulator") == "true")
        FixtureSafety.runToken = requireNotNull(arguments.getString("run_token")).also {
            require(it.matches(Regex("[a-f0-9]{32}")))
        }
        super.onCreate(arguments)
    }
}
