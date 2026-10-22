package io.github.bcrute.comicarchiver.runtime

import io.github.bcrute.comicarchiver.controller.BetaCodes
import io.github.bcrute.comicarchiver.worker.DownloadCommands
import org.junit.Assert.*
import org.junit.Test

class DownloadCommandsTest {
    private fun command(start: String, go: String, calls: MutableList<String>): DownloadCommands =
        DownloadCommands(object : PythonBridge {
            override fun dispatch(op: String, argsJson: String): String {
                calls.add(op)
                return if (op == "worker_start") start else go
            }
        }, "/workspace", "{}")

    @Test fun refuses_plan_admission_when_start_failed_or_worker_is_stopped_or_draining() {
        for (start in listOf("""{"ok":false,"code":"refused"}""",
            """{"ok":true,"result":{"running":false}}""",
            """{"ok":true,"result":{"running":true,"stop_requested":true}}""")) {
            val calls = mutableListOf<String>()
            assertEquals(BetaCodes.WORKER_OFFLINE, command(start, "{}", calls).go())
            assertEquals(listOf("worker_start"), calls)
        }
    }

    @Test fun ready_worker_does_not_hide_plan_failure() {
        val calls = mutableListOf<String>()
        val value = command("""{"ok":true,"result":{"running":true}}""",
            """{"ok":false,"code":"refused"}""", calls).go()
        assertEquals(BetaCodes.JOB_FAILED, value)
        assertEquals(listOf("worker_start", "go"), calls)
    }

    @Test fun admission_requires_both_successful_commands_in_order() {
        val calls = mutableListOf<String>()
        assertNull(command("""{"ok":true,"result":{"running":true}}""",
            """{"ok":true,"result":{}}""", calls).go())
        assertEquals(listOf("worker_start", "go"), calls)
    }
}
