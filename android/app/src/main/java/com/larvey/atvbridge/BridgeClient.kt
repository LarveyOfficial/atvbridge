package com.larvey.atvbridge

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import okhttp3.Call
import okhttp3.Callback
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONException
import org.json.JSONObject
import java.io.IOException
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

/**
 * WebSocket connection to the bridge (`/ws`) with automatic reconnect.
 * All listener callbacks run on the main thread.
 */
class BridgeClient(private val baseUrl: String, private val listener: Listener) {

    interface Listener {
        fun onBridgeConnected(connected: Boolean)
        fun onState(state: BridgeState)
        fun onArtwork(id: String, bitmap: Bitmap?)
    }

    private val http = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .pingInterval(15, TimeUnit.SECONDS)
        .build()
    private val main = Handler(Looper.getMainLooper())
    private val reconnect = Runnable { connect() }
    private var ws: WebSocket? = null
    private var open = false
    private var closed = false
    private var retryMs = INITIAL_RETRY_MS

    fun start() = connect()

    private fun connect() {
        if (closed) return
        main.removeCallbacks(reconnect)
        ws?.cancel()
        val request = Request.Builder().url("$baseUrl/ws").build()
        ws = http.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                main.post {
                    if (webSocket !== ws) return@post
                    open = true
                    retryMs = INITIAL_RETRY_MS
                    listener.onBridgeConnected(true)
                }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val receivedAt = SystemClock.elapsedRealtime()
                val json = try {
                    JSONObject(text)
                } catch (e: JSONException) {
                    return
                }
                if (json.optString("type") != "state") return
                val state = BridgeState.parse(json, receivedAt)
                main.post { if (webSocket === ws) listener.onState(state) }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                main.post { lost(webSocket) }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                main.post { lost(webSocket) }
            }
        })
    }

    private fun lost(webSocket: WebSocket) {
        if (webSocket !== ws) return
        ws = null
        if (open) {
            open = false
            listener.onBridgeConnected(false)
        }
        if (!closed) {
            main.postDelayed(reconnect, retryMs)
            retryMs = (retryMs * 2).coerceAtMost(MAX_RETRY_MS)
        }
    }

    /** Reconnect right away (e.g. after joining Wi-Fi) instead of waiting for the backoff. */
    fun kick() {
        if (!open && !closed) {
            retryMs = INITIAL_RETRY_MS
            connect()
        }
    }

    /** Send a bridge command such as "play_pause" or "seek" (with "position"). */
    fun send(command: String, vararg extra: Pair<String, Any>): Boolean {
        val socket = ws?.takeIf { open } ?: return false
        val msg = JSONObject().put("type", "command").put("command", command)
        for ((key, value) in extra) msg.put(key, value)
        return socket.send(msg.toString())
    }

    fun fetchArtwork(id: String) {
        val url = "$baseUrl/api/artwork?id=" + URLEncoder.encode(id, "UTF-8")
        http.newCall(Request.Builder().url(url).build()).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                main.post { if (!closed) listener.onArtwork(id, null) }
            }

            override fun onResponse(call: Call, response: Response) {
                val bitmap = response.use {
                    if (it.isSuccessful) it.body?.byteStream()?.let(BitmapFactory::decodeStream) else null
                }
                main.post { if (!closed) listener.onArtwork(id, bitmap) }
            }
        })
    }

    fun close() {
        closed = true
        main.removeCallbacks(reconnect)
        ws?.close(1000, null)
        ws = null
        open = false
        http.dispatcher.executorService.shutdown()
    }

    private companion object {
        const val INITIAL_RETRY_MS = 1_000L
        const val MAX_RETRY_MS = 30_000L
    }
}
