package com.atvbridge.companion

import org.json.JSONObject

data class NowPlaying(
    val title: String?,
    val artist: String?,
    val album: String?,
    /** playing, paused, loading, seeking, stopped or idle */
    val state: String,
    val positionMs: Long?,
    val durationMs: Long?,
    val artworkId: String?,
)

/** The parts of the bridge's "state" WebSocket message the app uses. */
data class BridgeState(
    val name: String,
    val atvConnected: Boolean,
    val power: String?,
    val error: String?,
    val appName: String?,
    val volume: Int?,
    val nowPlaying: NowPlaying?,
    /** SystemClock.elapsedRealtime() at which nowPlaying.positionMs was current. */
    val positionTime: Long,
) {
    companion object {
        fun parse(json: JSONObject, receivedAt: Long): BridgeState {
            val atv = json.optJSONObject("appletv") ?: JSONObject()
            val serverTime = json.num("server_time")
            val np = atv.optJSONObject("now_playing")?.let { n ->
                val state = n.str("state") ?: "idle"
                var position = n.num("position")
                val updatedAt = n.num("updated_at")
                // The position was sampled at updated_at; bring it up to "now" on the bridge's clock.
                if (position != null && state == "playing" && updatedAt != null && serverTime != null) {
                    position += (serverTime - updatedAt).coerceAtLeast(0.0)
                }
                NowPlaying(
                    title = n.str("title"),
                    artist = n.str("artist"),
                    album = n.str("album") ?: n.str("series"),
                    state = state,
                    positionMs = position?.let { (it * 1000).toLong() },
                    durationMs = n.num("total")?.let { (it * 1000).toLong() },
                    artworkId = n.str("artwork_id"),
                )
            }
            return BridgeState(
                name = json.str("name") ?: "Apple TV",
                atvConnected = atv.optBoolean("connected"),
                power = atv.str("power"),
                error = atv.str("error"),
                appName = atv.optJSONObject("app")?.str("name"),
                volume = atv.num("volume")?.toInt(),
                nowPlaying = np,
                positionTime = receivedAt,
            )
        }
    }
}

// org.json turns JSON null into the string "null"; treat null/missing/blank as absent.
private fun JSONObject.str(key: String): String? =
    if (isNull(key)) null else optString(key).takeIf { it.isNotBlank() }

private fun JSONObject.num(key: String): Double? =
    if (isNull(key)) null else optDouble(key).takeIf { !it.isNaN() }
