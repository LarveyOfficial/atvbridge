package com.larvey.atvbridge

import android.content.Context
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("atvbridge", Context.MODE_PRIVATE)

    /** Base URL of the bridge, e.g. "http://192.168.0.10:8090". */
    var bridgeUrl: String?
        get() = sp.getString("bridge_url", null)
        set(value) = sp.edit().putString("bridge_url", value).apply()

    /** Whether the user wants the service running (restored after reboot/update). */
    var enabled: Boolean
        get() = sp.getBoolean("enabled", false)
        set(value) = sp.edit().putBoolean("enabled", value).apply()

    var startOnBoot: Boolean
        get() = sp.getBoolean("start_on_boot", true)
        set(value) = sp.edit().putBoolean("start_on_boot", value).apply()

    /** Route the phone's volume buttons to the Apple TV while its media card is active. */
    var volumeKeys: Boolean
        get() = sp.getBoolean("volume_keys", true)
        set(value) = sp.edit().putBoolean("volume_keys", value).apply()

    companion object {
        const val DEFAULT_PORT = 8090
        private val EXPLICIT_PORT = Regex("^[a-zA-Z]+://(\\[[^]]+]|[^/:]+):\\d+")

        /** Accepts "192.168.0.10", "192.168.0.10:8090" or a full URL; returns "http://host:port". */
        fun normalizeUrl(input: String): String? {
            var s = input.trim().trimEnd('/')
            if (s.isEmpty()) return null
            if (!s.contains("://")) s = "http://$s"
            val url = s.toHttpUrlOrNull() ?: return null
            val port = if (EXPLICIT_PORT.containsMatchIn(s)) url.port else DEFAULT_PORT
            return HttpUrl.Builder().scheme(url.scheme).host(url.host).port(port).build()
                .toString().trimEnd('/')
        }
    }
}
