package com.atvbridge.companion

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.drawable.Icon
import android.media.AudioAttributes
import android.media.MediaMetadata
import android.media.VolumeProvider
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.net.ConnectivityManager
import android.net.Network
import android.os.Bundle
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log

/**
 * Keeps a WebSocket to the bridge and mirrors the Apple TV's now-playing state into a
 * MediaSession, which gives the native lock screen / Quick Settings media card.
 */
class BridgeService : Service(), BridgeClient.Listener {

    private lateinit var prefs: Prefs
    private lateinit var notifications: NotificationManager
    private lateinit var session: MediaSession
    private val main = Handler(Looper.getMainLooper())
    private var client: BridgeClient? = null
    private var bridgeConnected = false
    private var state: BridgeState? = null
    private var artworkId: String? = null
    private var artwork: Bitmap? = null
    private var volumeProvider: VolumeProvider? = null

    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            main.post { client?.kick() }
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        prefs = Prefs(this)
        notifications = getSystemService(NotificationManager::class.java)
        createChannels()
        session = MediaSession(this, "ATV Bridge").apply {
            setCallback(sessionCallback)
            setSessionActivity(openAppIntent())
        }
        try {
            startForeground(
                NOTIFICATION_ID,
                statusNotification(getString(R.string.status_connecting)),
                ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE,
            )
        } catch (e: Exception) {
            // e.g. ForegroundServiceStartNotAllowedException when restarted from the background.
            Log.w(TAG, "Could not start in foreground", e)
            stopSelf()
            return
        }
        val url = prefs.bridgeUrl
        if (url == null) {
            stopSelf()
            return
        }
        connect(url)
        getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(networkCallback)
    }

    private fun connect(url: String) {
        client?.close()
        bridgeConnected = false
        state = null
        artworkId = null
        artwork = null
        client = BridgeClient(url, this).also { it.start() }
        update()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                prefs.enabled = false
                stopSelf()
            }
            ACTION_COMMAND -> intent.getStringExtra(EXTRA_COMMAND)?.let { command(it) }
            ACTION_RECONNECT -> prefs.bridgeUrl?.let { connect(it) }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        client?.close()
        client = null
        runCatching {
            getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(networkCallback)
        }
        session.release()
        publish(null)
        super.onDestroy()
    }

    // --- bridge -> media session ----------------------------------------------------------

    override fun onBridgeConnected(connected: Boolean) {
        bridgeConnected = connected
        if (!connected) state = null
        update()
    }

    override fun onState(state: BridgeState) {
        this.state = state
        val id = state.nowPlaying?.artworkId
        if (id != artworkId) {
            artworkId = id
            artwork = null
            if (id != null) client?.fetchArtwork(id)
        }
        update()
    }

    override fun onArtwork(id: String, bitmap: Bitmap?) {
        if (id == artworkId) {
            artwork = bitmap
            update()
        }
    }

    private fun update() {
        val st = state
        val np = st?.nowPlaying?.takeIf { st.atvConnected && it.state != "idle" && it.state != "stopped" }
        if (st == null || np == null) {
            session.isActive = false
            session.setPlaybackState(PlaybackState.Builder().setState(PlaybackState.STATE_NONE, 0, 0f).build())
            val text = when {
                !bridgeConnected || st == null -> getString(R.string.status_connecting)
                !st.atvConnected -> getString(R.string.status_atv_offline, st.name, st.error ?: "connecting…")
                st.power == "off" -> getString(R.string.status_asleep, st.name)
                else -> getString(R.string.status_idle, st.name)
            }
            notifications.notify(NOTIFICATION_ID, statusNotification(text))
            publish(text)
            return
        }

        val title = np.title ?: st.appName ?: st.name
        val subtitle = listOfNotNull(np.artist, np.album).joinToString(" — ").ifEmpty { st.appName ?: st.name }
        val duration = np.durationMs ?: 0
        session.setMetadata(
            MediaMetadata.Builder()
                .putString(MediaMetadata.METADATA_KEY_TITLE, title)
                .putString(MediaMetadata.METADATA_KEY_ARTIST, subtitle)
                .putString(MediaMetadata.METADATA_KEY_ALBUM, np.album)
                .putLong(MediaMetadata.METADATA_KEY_DURATION, if (duration > 0) duration else -1)
                .apply { artwork?.let { putBitmap(MediaMetadata.METADATA_KEY_ART, it) } }
                .build(),
        )

        val playing = np.state == "playing"
        val playbackState = when (np.state) {
            "playing" -> PlaybackState.STATE_PLAYING
            "paused" -> PlaybackState.STATE_PAUSED
            "loading", "seeking" -> PlaybackState.STATE_BUFFERING
            else -> PlaybackState.STATE_STOPPED
        }
        var actions = PlaybackState.ACTION_PLAY or PlaybackState.ACTION_PAUSE or
            PlaybackState.ACTION_PLAY_PAUSE or PlaybackState.ACTION_STOP or
            PlaybackState.ACTION_SKIP_TO_NEXT or PlaybackState.ACTION_SKIP_TO_PREVIOUS or
            PlaybackState.ACTION_FAST_FORWARD or PlaybackState.ACTION_REWIND
        if (duration > 0) actions = actions or PlaybackState.ACTION_SEEK_TO
        session.setPlaybackState(
            PlaybackState.Builder()
                .setActions(actions)
                .setState(
                    playbackState,
                    np.positionMs ?: PlaybackState.PLAYBACK_POSITION_UNKNOWN,
                    if (playing) 1f else 0f,
                    st.positionTime,
                )
                .addCustomAction(
                    PlaybackState.CustomAction.Builder(CUSTOM_REWIND, getString(R.string.rewind), R.drawable.ic_replay).build(),
                )
                .addCustomAction(
                    PlaybackState.CustomAction.Builder(CUSTOM_FORWARD, getString(R.string.forward), R.drawable.ic_forward).build(),
                )
                .build(),
        )
        updateVolume(st)
        session.isActive = true
        notifications.notify(NOTIFICATION_ID, mediaNotification(title, subtitle, playing))
        publish(getString(if (playing) R.string.status_playing else R.string.status_paused, title))
    }

    private fun updateVolume(st: BridgeState) {
        // Apple TVs driving the TV over HDMI-CEC/IR can only step up/down (no level),
        // so that case gets a relative provider; the shown level is a placeholder.
        val mode = when {
            !prefs.volumeKeys -> null
            st.volumeControl == "absolute" && st.volume != null -> VolumeProvider.VOLUME_CONTROL_ABSOLUTE
            st.volumeControl == "relative" || st.volumeControl == "absolute" -> VolumeProvider.VOLUME_CONTROL_RELATIVE
            else -> null
        }
        if (mode == null) {
            if (volumeProvider != null) {
                volumeProvider = null
                session.setPlaybackToLocal(
                    AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).build(),
                )
            }
            return
        }
        val provider = volumeProvider?.takeIf { it.volumeControl == mode }
            ?: object : VolumeProvider(mode, 100, st.volume ?: 50) {
                override fun onSetVolumeTo(volume: Int) {
                    currentVolume = volume.coerceIn(0, 100)
                    command("set_volume", "level" to currentVolume)
                }

                override fun onAdjustVolume(direction: Int) {
                    when (direction) {
                        1 -> command("volume_up")
                        -1 -> command("volume_down")
                    }
                }
            }.also {
                volumeProvider = it
                session.setPlaybackToRemote(it)
            }
        if (mode == VolumeProvider.VOLUME_CONTROL_ABSOLUTE && st.volume != null && provider.currentVolume != st.volume) {
            provider.currentVolume = st.volume
        }
    }

    // --- media session -> bridge ----------------------------------------------------------

    private val sessionCallback = object : MediaSession.Callback() {
        override fun onPlay() = command("play")
        override fun onPause() = command("pause")
        override fun onStop() = command("stop")
        override fun onSkipToNext() = command("next")
        override fun onSkipToPrevious() = command("previous")
        override fun onFastForward() = command("skip_forward")
        override fun onRewind() = command("skip_backward")
        override fun onSeekTo(pos: Long) = command("seek", "position" to pos / 1000)

        override fun onCustomAction(action: String, extras: Bundle?) {
            when (action) {
                CUSTOM_REWIND -> command("skip_backward")
                CUSTOM_FORWARD -> command("skip_forward")
            }
        }
    }

    private fun command(name: String, vararg extra: Pair<String, Any>) {
        if (client?.send(name, *extra) != true) Log.w(TAG, "Bridge not connected; dropped $name")
    }

    // --- notifications ----------------------------------------------------------------------

    private fun createChannels() {
        notifications.createNotificationChannel(
            NotificationChannel(CHANNEL_MEDIA, getString(R.string.channel_media), NotificationManager.IMPORTANCE_LOW)
                .apply { setShowBadge(false) },
        )
        notifications.createNotificationChannel(
            NotificationChannel(CHANNEL_STATUS, getString(R.string.channel_status), NotificationManager.IMPORTANCE_MIN)
                .apply { setShowBadge(false) },
        )
    }

    private fun statusNotification(text: String): Notification =
        Notification.Builder(this, CHANNEL_STATUS)
            .setSmallIcon(R.drawable.ic_tv)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setContentIntent(openAppIntent())
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .addAction(
                Notification.Action.Builder(
                    Icon.createWithResource(this, R.drawable.ic_stop),
                    getString(R.string.disconnect),
                    servicePendingIntent(Intent(this, BridgeService::class.java).setAction(ACTION_STOP), 0),
                ).build(),
            )
            .build()

    private fun mediaNotification(title: String, subtitle: String, playing: Boolean): Notification {
        val builder = Notification.Builder(this, CHANNEL_MEDIA)
            .setSmallIcon(R.drawable.ic_tv)
            .setContentTitle(title)
            .setContentText(subtitle)
            .setContentIntent(openAppIntent())
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            .setCategory(Notification.CATEGORY_TRANSPORT)
            .addAction(commandAction(R.drawable.ic_prev, "Previous", "previous"))
            .addAction(
                if (playing) commandAction(R.drawable.ic_pause, "Pause", "pause")
                else commandAction(R.drawable.ic_play, "Play", "play"),
            )
            .addAction(commandAction(R.drawable.ic_next, "Next", "next"))
            .setStyle(
                Notification.MediaStyle()
                    .setMediaSession(session.sessionToken)
                    .setShowActionsInCompactView(0, 1, 2),
            )
        artwork?.let { builder.setLargeIcon(it) }
        return builder.build()
    }

    private fun commandAction(icon: Int, title: String, command: String): Notification.Action {
        val intent = Intent(this, BridgeService::class.java)
            .setAction(ACTION_COMMAND)
            .putExtra(EXTRA_COMMAND, command)
        return Notification.Action.Builder(
            Icon.createWithResource(this, icon), title, servicePendingIntent(intent, command.hashCode()),
        ).build()
    }

    private fun servicePendingIntent(intent: Intent, requestCode: Int): PendingIntent =
        PendingIntent.getService(
            this, requestCode, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

    private fun openAppIntent(): PendingIntent =
        PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )

    companion object {
        private const val TAG = "BridgeService"
        private const val NOTIFICATION_ID = 1
        private const val CHANNEL_MEDIA = "media"
        private const val CHANNEL_STATUS = "status"
        private const val ACTION_STOP = "com.atvbridge.companion.STOP"
        private const val ACTION_COMMAND = "com.atvbridge.companion.COMMAND"
        private const val ACTION_RECONNECT = "com.atvbridge.companion.RECONNECT"
        private const val EXTRA_COMMAND = "command"
        private const val CUSTOM_REWIND = "rewind10"
        private const val CUSTOM_FORWARD = "forward10"

        /** Human-readable status for the activity; null while the service isn't running. */
        var status: String? = null
            private set
        val statusListeners = mutableSetOf<() -> Unit>()

        private fun publish(text: String?) {
            status = text
            statusListeners.toList().forEach { it() }
        }

        val isRunning get() = status != null

        fun start(context: Context) {
            context.startForegroundService(Intent(context, BridgeService::class.java))
        }

        /** Reconnect a running service to the (changed) bridge URL. */
        fun reconnect(context: Context) {
            context.startService(Intent(context, BridgeService::class.java).setAction(ACTION_RECONNECT))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, BridgeService::class.java))
        }
    }
}
