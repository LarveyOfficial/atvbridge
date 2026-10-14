package com.larvey.atvbridge

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
import android.media.MediaMetadata
import android.media.MediaRoute2Info
import android.media.MediaRouter2
import android.media.RouteDiscoveryPreference
import android.media.VolumeProvider
import android.media.session.MediaSession
import android.media.session.PlaybackState
import android.net.ConnectivityManager
import android.net.Network
import android.os.Build
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
    /** Text shown in the keyboard notification, or null when it isn't shown. */
    private var keyboardShown: String? = null
    private var router: Any? = null  // MediaRouter2 (API 30+)
    private var routeCallback: Any? = null

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
        createChannels(this)
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
        RouteProviderService.onVolumeSet = { level -> command("set_volume", "level" to level) }
        if (Build.VERSION.SDK_INT >= 30) startRouting()
    }

    // --- media route (shows the Apple TV as the playback device) ---------------------------

    @android.annotation.TargetApi(30)
    private fun startRouting() {
        val r = MediaRouter2.getInstance(this)
        val callback = object : MediaRouter2.RouteCallback() {
            @Deprecated("Deprecated in API 34")
            override fun onRoutesAdded(routes: List<MediaRoute2Info>) = transferToAppleTv()

            @Deprecated("Deprecated in API 34")
            override fun onRoutesChanged(routes: List<MediaRoute2Info>) = transferToAppleTv()
        }
        r.registerRouteCallback(
            mainExecutor, callback,
            RouteDiscoveryPreference.Builder(listOf(RouteProviderService.FEATURE), true).build(),
        )
        r.registerTransferCallback(mainExecutor, object : MediaRouter2.TransferCallback() {
            override fun onTransfer(oldController: MediaRouter2.RoutingController, newController: MediaRouter2.RoutingController) {
                Log.i(TAG, "Media routed to ${newController.selectedRoutes.map { it.name }}")
            }

            override fun onTransferFailure(requestedRoute: MediaRoute2Info) {
                Log.w(TAG, "Routing to ${requestedRoute.name} failed")
            }
        })
        router = r
        routeCallback = callback
        transferToAppleTv()
    }

    @android.annotation.TargetApi(30)
    private fun transferToAppleTv() {
        val r = router as? MediaRouter2 ?: return
        val route = r.routes.firstOrNull { RouteProviderService.FEATURE in it.features } ?: return
        val routed = r.controllers.any { c -> !c.isReleased && c.selectedRoutes.any { it.id == route.id } }
        if (!routed) r.transferTo(route)
    }

    @android.annotation.TargetApi(30)
    private fun stopRouting() {
        val r = router as? MediaRouter2 ?: return
        r.controllers.filter { it != r.systemController }.forEach { it.release() }
        (routeCallback as? MediaRouter2.RouteCallback)?.let { r.unregisterRouteCallback(it) }
        router = null
        routeCallback = null
    }

    private fun connect(url: String) {
        client?.close()
        bridgeConnected = false
        state = null
        artworkId = null
        artwork = null
        client = BridgeClient(url, this).also { it.start() }
        session.setSessionActivity(tapIntent())
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
        if (Build.VERSION.SDK_INT >= 30) stopRouting()
        RouteProviderService.onVolumeSet = null
        client?.close()
        client = null
        runCatching {
            getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(networkCallback)
        }
        session.release()
        notifications.cancel(KEYBOARD_NOTIFICATION_ID)
        publish(null)
        super.onDestroy()
    }

    // --- bridge -> media session ----------------------------------------------------------

    override fun onBridgeConnected(connected: Boolean) {
        bridgeConnected = connected
        if (!connected) state = null
        update()
        updateKeyboardNotification()
    }

    override fun onState(state: BridgeState) {
        this.state = state
        if (Build.VERSION.SDK_INT >= 30) RouteProviderService.setDeviceName(state.name)
        updateKeyboardNotification()
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
                // Asleep or idle: just say it's connected (no "asleep" notification).
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
        // Always a remote session, so Android attributes playback to the Apple TV.
        // Most Apple TVs change the TV's volume with HDMI-CEC/IR presses and can't read a level:
        // then volume is RELATIVE, i.e. each volume key press is exactly one press on the TV and
        // Android ignores slider drags (MediaSessionRecord.setVolumeTo). Only if the Apple TV
        // reports a real level do we offer ABSOLUTE control with a working slider.
        val absolute = st.volumeControl == "absolute" && st.volume != null
        val mode = when {
            !prefs.volumeKeys || st.volumeControl == null -> VolumeProvider.VOLUME_CONTROL_FIXED
            absolute -> VolumeProvider.VOLUME_CONTROL_ABSOLUTE
            else -> VolumeProvider.VOLUME_CONTROL_RELATIVE
        }
        val level = if (absolute) st.volume!! else 50
        val provider = volumeProvider?.takeIf { it.volumeControl == mode }
            ?: newVolumeProvider(mode, level).also {
                volumeProvider = it
                session.setPlaybackToRemote(it)
            }
        if (absolute && provider.currentVolume != level) provider.currentVolume = level
        if (Build.VERSION.SDK_INT >= 30) {
            RouteProviderService.setVolume(if (mode == VolumeProvider.VOLUME_CONTROL_ABSOLUTE) level else null)
        }
    }

    private fun newVolumeProvider(mode: Int, current: Int): VolumeProvider {
        val callbacks = object {
            fun set(provider: VolumeProvider, volume: Int) {
                provider.currentVolume = volume.coerceIn(0, 100)
                command("set_volume", "level" to provider.currentVolume)
            }

            fun adjust(direction: Int) {
                when (direction) {
                    1 -> command("volume_up")  // AudioManager.ADJUST_RAISE: one press
                    -1 -> command("volume_down")  // ADJUST_LOWER
                    100, -100, 101 -> command("mute")  // ADJUST_UNMUTE / MUTE / TOGGLE_MUTE
                }
            }
        }
        // The id ties this session to RouteProviderService's routing session (Android 11+).
        return if (Build.VERSION.SDK_INT >= 30) {
            object : VolumeProvider(mode, 100, current, RouteProviderService.SESSION_ID) {
                override fun onSetVolumeTo(volume: Int) = callbacks.set(this, volume)
                override fun onAdjustVolume(direction: Int) = callbacks.adjust(direction)
            }
        } else {
            object : VolumeProvider(mode, 100, current) {
                override fun onSetVolumeTo(volume: Int) = callbacks.set(this, volume)
                override fun onAdjustVolume(direction: Int) = callbacks.adjust(direction)
            }
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

    /** Shown while the Apple TV waits for text; tapping opens the Google TV remote to type. */
    private fun updateKeyboardNotification() {
        val st = state
        if (!bridgeConnected || st == null || !st.atvConnected || !st.keyboardFocused) {
            if (keyboardShown != null) {
                notifications.cancel(KEYBOARD_NOTIFICATION_ID)
                keyboardShown = null
            }
            return
        }
        val text = st.keyboardText.orEmpty()
        if (keyboardShown == text) return
        keyboardShown = text
        val body = if (text.isEmpty()) getString(R.string.keyboard_body) else getString(R.string.keyboard_body_text, text)
        val notification = Notification.Builder(this, CHANNEL_KEYBOARD)
            .setSmallIcon(R.drawable.ic_keyboard)
            .setContentTitle(getString(R.string.keyboard_title, st.name))
            .setContentText(body)
            .setStyle(Notification.BigTextStyle().bigText(body))
            .setContentIntent(tapIntent())
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setShowWhen(false)
            .setCategory(Notification.CATEGORY_STATUS)
            .setVisibility(Notification.VISIBILITY_PUBLIC)
            // Live Update (status bar chip), Android 16 QPR2+ / 17: requestPromotedOngoing, ongoing,
            // title, BigTextStyle, and NOT colorized (Notification.hasPromotableCharacteristics).
            // Set via extras since this app compiles against API 35.
            .addExtras(Bundle().apply {
                putBoolean("android.requestPromotedOngoing", true)
                putString("android.shortCriticalText", getString(R.string.keyboard_chip))
            })
            .build()
        notifications.notify(KEYBOARD_NOTIFICATION_ID, notification)
    }

    private fun mediaNotification(title: String, subtitle: String, playing: Boolean): Notification {
        val builder = Notification.Builder(this, CHANNEL_MEDIA)
            .setSmallIcon(R.drawable.ic_tv)
            .setContentTitle(title)
            .setContentText(subtitle)
            .setContentIntent(tapIntent())
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

    /** Tapping the media card or the keyboard notification opens the Google TV remote. */
    private fun tapIntent(): PendingIntent =
        PendingIntent.getActivity(
            this, 1, Intent(this, RemoteLauncherActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )

    private fun openAppIntent(): PendingIntent =
        PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )

    companion object {
        private const val TAG = "BridgeService"
        private const val NOTIFICATION_ID = 1
        private const val KEYBOARD_NOTIFICATION_ID = 2
        private const val CHANNEL_KEYBOARD = "keyboard"
        private const val CHANNEL_MEDIA = "media"
        /**
         * The foreground service's notification while nothing is playing. Android requires one to
         * stay connected in the background; this channel's importance is NONE, so it never shows.
         * (Replaces the old visible "status" channel.)
         */
        private const val CHANNEL_STATUS = "connection"
        private const val OLD_CHANNEL_STATUS = "status"
        private const val ACTION_STOP = "com.larvey.atvbridge.STOP"
        private const val ACTION_COMMAND = "com.larvey.atvbridge.COMMAND"
        private const val ACTION_RECONNECT = "com.larvey.atvbridge.RECONNECT"
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

        private fun createChannels(context: Context) {
            val notifications = context.getSystemService(NotificationManager::class.java)
            notifications.createNotificationChannel(
                NotificationChannel(CHANNEL_MEDIA, context.getString(R.string.channel_media), NotificationManager.IMPORTANCE_LOW)
                    .apply { setShowBadge(false) },
            )
            runCatching { notifications.deleteNotificationChannel(OLD_CHANNEL_STATUS) }
            notifications.createNotificationChannel(
                NotificationChannel(CHANNEL_STATUS, context.getString(R.string.channel_status), NotificationManager.IMPORTANCE_NONE)
                    .apply { setShowBadge(false) },
            )
            notifications.createNotificationChannel(
                NotificationChannel(CHANNEL_KEYBOARD, context.getString(R.string.channel_keyboard), NotificationManager.IMPORTANCE_HIGH)
                    .apply {
                        setShowBadge(false)
                        setSound(null, null)
                    },
            )
        }

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
