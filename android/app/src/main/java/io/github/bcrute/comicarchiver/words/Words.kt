package io.github.bcrute.comicarchiver.words

import org.json.JSONObject

/**
 * Every phrase the app shows a person, by code. The words are the desktop's own: the build packs
 * the core's messages.json, so a phrase is written once and both apps say it the same way.
 */
object Words {
    private val text: Map<String, String> by lazy {
        val stream = checkNotNull(Words::class.java.getResourceAsStream("/messages.json")) { "The app's words are missing." }
        val json = JSONObject(stream.use { it.readBytes().toString(Charsets.UTF_8) })
        json.keys().asSequence().associateWith { json.getString(it) }
    }

    /** The phrase for [code], with each named value filled in; a code without words shows itself. */
    fun say(code: String, vararg values: Pair<String, Any?>): String {
        var words = text[code] ?: return code
        for ((name, value) in values) words = words.replace("{$name}", value.toString())
        return words
    }
}

fun say(code: String, vararg values: Pair<String, Any?>): String = Words.say(code, *values)
