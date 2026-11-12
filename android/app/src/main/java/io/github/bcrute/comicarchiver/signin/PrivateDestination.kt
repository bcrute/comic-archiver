package io.github.bcrute.comicarchiver.signin

/**
 * The desktop sign-in window's rule for where a sign-in page may send a request: public
 * identity providers are fine, but the page has no reason to reach this phone, another
 * device on the local network or a special-purpose address. Names are judged as written,
 * before any lookup; nothing here asks the network a question.
 */
object PrivateDestination {
    private val QUAD = Regex("""\d{1,3}(\.\d{1,3}){3}""")
    private val NUMBER = Regex("""\d+|0x[0-9a-f]*""")

    fun matches(host: String?): Boolean {
        val name = (host ?: return false).trim().removePrefix("[").removeSuffix("]").trimEnd('.').lowercase()
        if (name == "localhost" || name.endsWith(".localhost") || name == "local" || name.endsWith(".local") ||
            name == "home.arpa" || name.endsWith(".home.arpa")) return true
        // A literal is parsed here, never handed to anything that could look it up; one
        // that does not parse is refused.
        return when {
            ':' in name -> v6(name)?.let(::privateV6) ?: true
            QUAD.matches(name) -> v4(name)?.let(::privateV4) ?: true
            // No public name ends in a number: an address written another way is refused.
            else -> NUMBER.matches(name.substringAfterLast('.'))
        }
    }

    private fun v4(text: String): List<Int>? =
        text.split('.').map { it.toInt() }.takeIf { parts -> parts.all { it <= 255 } }

    /** The sixteen-bit groups of an IPv6 literal, or null when it is not one. */
    private fun v6(text: String): List<Int>? {
        var body = text
        val tail = mutableListOf<Int>()
        if ('.' in body) {
            val quad = body.substringAfterLast(':')
            val parts = quad.takeIf { QUAD.matches(it) }?.let(::v4) ?: return null
            tail += (parts[0] shl 8) or parts[1]
            tail += (parts[2] shl 8) or parts[3]
            body = body.substring(0, body.length - quad.length) + "0:0"
        }
        fun groups(part: String): List<Int>? {
            if (part.isEmpty()) return emptyList()
            return part.split(':').map {
                if (it.isEmpty() || it.length > 4) return null
                it.toIntOrNull(16) ?: return null
            }
        }
        val halves = body.split("::")
        val all = when (halves.size) {
            1 -> groups(halves[0])?.takeIf { it.size == 8 }
            2 -> {
                val head = groups(halves[0]) ?: return null
                val rest = groups(halves[1]) ?: return null
                if (head.size + rest.size > 7) null else head + List(8 - head.size - rest.size) { 0 } + rest
            }
            else -> null
        } ?: return null
        return if (tail.isEmpty()) all else all.dropLast(2) + tail
    }

    private fun privateV4(b: List<Int>): Boolean {
        val a = b[0]
        val c = b[1]
        val d = b[2]
        return a == 0 || a == 10 || a == 127 || a >= 224 ||
            a == 100 && c in 64..127 ||
            a == 169 && c == 254 ||
            a == 172 && c in 16..31 ||
            a == 192 && c == 168 ||
            a == 192 && c == 0 && (d == 0 || d == 2) ||
            a == 198 && (c == 18 || c == 19) ||
            a == 198 && c == 51 && d == 100 ||
            a == 203 && c == 0 && d == 113
    }

    private fun privateV6(groups: List<Int>): Boolean {
        // An IPv4 address carried in an IPv6 one is judged as the IPv4 address it is.
        if (groups.take(5).all { it == 0 } && groups[5] == 0xffff)
            return privateV4(listOf(groups[6] shr 8, groups[6] and 0xff, groups[7] shr 8, groups[7] and 0xff))
        val first = groups[0]
        val second = groups[1]
        // Only the public unicast block, less its special-purpose and documentation parts.
        if (first and 0xe000 != 0x2000) return true
        return first == 0x2001 && (second < 0x0200 || second == 0x0db8) ||
            first == 0x2002 || first and 0xfff0 == 0x3ff0
    }
}
