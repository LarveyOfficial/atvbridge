package com.larvey.atvbridge

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class BridgeStateTest {

    // Shape of the bridge's /ws "state" message (see bridge/atvbridge/web.py snapshot()).
    private val playing = """
        {"type": "state", "name": "Chomusuke", "server_time": 1000.5,
         "appletv": {"connected": true, "error": null, "name": "Chomusuke", "power": "on",
                     "volume": 50.0, "volume_control": "absolute", "keyboard": true,
                     "keyboard_text": "stra", "app": {"name": "YouTube", "id": "com.google.ios.youtube"},
                     "now_playing": {"title": "Video", "artist": "Channel", "album": "", "series": null,
                                     "state": "playing", "position": 105, "total": 625,
                                     "media_type": "unknown", "updated_at": 998.5, "artwork_id": null}},
         "phones": [], "remotes_connected": 0}
    """.trimIndent()

    @Test
    fun parsesNowPlaying() {
        val st = BridgeState.parse(JSONObject(playing), receivedAt = 42)
        assertEquals("Chomusuke", st.name)
        assertTrue(st.atvConnected)
        assertEquals("on", st.power)
        assertNull(st.error)
        assertEquals("YouTube", st.appName)
        assertEquals(50, st.volume)
        assertEquals("absolute", st.volumeControl)
        assertTrue(st.keyboardFocused)
        assertEquals("stra", st.keyboardText)
        assertEquals(42L, st.positionTime)
        val np = st.nowPlaying!!
        assertEquals("Video", np.title)
        assertEquals("Channel", np.artist)
        assertNull("blank album and null series are absent", np.album)
        assertNull("JSON null must not become \"null\"", np.artworkId)
        assertEquals("playing", np.state)
        assertEquals(625_000L, np.durationMs)
        // Position sampled 2 s before server_time while playing -> advanced by 2 s.
        assertEquals(107_000L, np.positionMs)
    }

    @Test
    fun pausedPositionIsNotAdvanced() {
        val json = JSONObject(playing)
        json.getJSONObject("appletv").getJSONObject("now_playing").put("state", "paused")
        assertEquals(105_000L, BridgeState.parse(json, 0).nowPlaying!!.positionMs)
    }

    @Test
    fun seriesUsedWhenNoAlbum() {
        val json = JSONObject(playing)
        json.getJSONObject("appletv").getJSONObject("now_playing").put("series", "The Show")
        assertEquals("The Show", BridgeState.parse(json, 0).nowPlaying!!.album)
    }

    @Test
    fun nothingPlayingAndDisconnected() {
        val json = JSONObject(
            """{"type": "state", "name": "Apple TV",
                "appletv": {"connected": false, "error": "Not configured", "now_playing": null}}""",
        )
        val st = BridgeState.parse(json, 0)
        assertFalse(st.atvConnected)
        assertEquals("Not configured", st.error)
        assertNull(st.nowPlaying)
        assertNull(st.volume)
        assertNull(st.volumeControl)
        assertFalse(st.keyboardFocused)
        assertNull(st.keyboardText)
        assertNull(st.appName)
    }
}
