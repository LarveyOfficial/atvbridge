package com.atvbridge.companion

import android.annotation.TargetApi
import android.media.MediaRoute2Info
import android.media.MediaRoute2ProviderService
import android.media.RoutingSessionInfo
import android.os.Build
import android.os.Bundle

/**
 * Publishes the Apple TV as a media route, so Android shows our media card as playing on it
 * (output switcher chip with its name) instead of "This device".
 *
 * System UI names a remote MediaSession after the routing session whose id matches the
 * session's VolumeProvider volumeControlId and whose owner is the same app
 * (MediaRouter2Manager.getRoutingSessionForMediaController). BridgeService transfers its own
 * playback to this route, which creates that session, and uses SESSION_ID as volumeControlId.
 */
@TargetApi(Build.VERSION_CODES.R)
class RouteProviderService : MediaRoute2ProviderService() {

    override fun onCreate() {
        super.onCreate()
        instance = this
        publish()
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    private fun publish() {
        notifyRoutes(listOf(route()))
        sessionOwner?.let { notifySessionUpdated(session(it)) }
    }

    private fun route(): MediaRoute2Info =
        MediaRoute2Info.Builder(ROUTE_ID, deviceName)
            .addFeature(FEATURE)
            .addFeature(MediaRoute2Info.FEATURE_REMOTE_PLAYBACK)
            .setVolumeHandling(MediaRoute2Info.PLAYBACK_VOLUME_VARIABLE)
            .setVolumeMax(VOLUME_MAX)
            .setVolume(VOLUME_MID)
            .setConnectionState(MediaRoute2Info.CONNECTION_STATE_CONNECTED)
            .apply { if (Build.VERSION.SDK_INT >= 34) setType(MediaRoute2Info.TYPE_REMOTE_TV) }
            .build()

    private fun session(owner: String): RoutingSessionInfo =
        RoutingSessionInfo.Builder(SESSION_ID, owner)
            .addSelectedRoute(ROUTE_ID)
            .setName(deviceName)
            .setVolumeHandling(MediaRoute2Info.PLAYBACK_VOLUME_VARIABLE)
            .setVolumeMax(VOLUME_MAX)
            .setVolume(VOLUME_MID)
            .build()

    override fun onCreateSession(requestId: Long, packageName: String, routeId: String, sessionHints: Bundle?) {
        if (routeId != ROUTE_ID) {
            notifyRequestFailed(requestId, REASON_ROUTE_NOT_AVAILABLE)
            return
        }
        sessionOwner = packageName
        notifySessionCreated(requestId, session(packageName))
    }

    override fun onReleaseSession(requestId: Long, sessionId: String) {
        sessionOwner = null
        notifySessionReleased(sessionId)
    }

    // The output switcher's volume slider: the Apple TV can only step, so treat the change
    // from the middle as steps and snap back.
    override fun onSetRouteVolume(requestId: Long, routeId: String, volume: Int) = volumeStep(volume)
    override fun onSetSessionVolume(requestId: Long, sessionId: String, volume: Int) = volumeStep(volume)

    private fun volumeStep(volume: Int) {
        onVolumeSteps?.invoke(volume - VOLUME_MID)
        publish()
    }

    override fun onSelectRoute(requestId: Long, sessionId: String, routeId: String) {}
    override fun onDeselectRoute(requestId: Long, sessionId: String, routeId: String) {}
    override fun onTransferToRoute(requestId: Long, sessionId: String, routeId: String) {}

    companion object {
        const val FEATURE = "com.atvbridge.companion.FEATURE_APPLE_TV"
        const val ROUTE_ID = "appletv"
        /** Routing session id; also the media session VolumeProvider's volumeControlId. */
        const val SESSION_ID = "atvbridge"
        private const val VOLUME_MAX = 100
        private const val VOLUME_MID = 50

        private var instance: RouteProviderService? = null
        private var sessionOwner: String? = null
        private var deviceName = "Apple TV"

        /** Set by BridgeService: change of the output switcher volume slider, in steps. */
        var onVolumeSteps: ((Int) -> Unit)? = null

        fun setDeviceName(name: String) {
            if (name == deviceName) return
            deviceName = name
            instance?.publish()
        }
    }
}
