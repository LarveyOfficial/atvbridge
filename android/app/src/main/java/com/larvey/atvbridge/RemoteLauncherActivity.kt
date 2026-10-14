package com.larvey.atvbridge

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Bundle
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/**
 * Opens the Google TV app's remote, connected to the bridge (falls back to this app).
 *
 * Taps on the media card and notifications go through this invisible activity instead of
 * straight to Google's: its entry activity may show over the lock screen but hands off to a
 * remote screen that can't, so the remote only appeared after unlocking. This activity isn't
 * shown over the lock screen, so the system asks to unlock first and the remote opens right
 * after. Clearing the remote's task also stops repeated taps from stacking remotes.
 */
class RemoteLauncherActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val remote = remoteIntent(this)
        startActivity(
            if (remote.resolveActivity(packageManager) != null) remote
            else Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
        finish()
    }

    companion object {
        private const val GOOGLE_TV_PACKAGE = "com.google.android.videos"
        /** Google TV app's DeviceNotificationActivity, the entry point Google TVs' own notifications use. */
        private const val GOOGLE_TV_REMOTE_ACTION = "com.google.android.apps.googletv.ACTION_VIRTUAL_REMOTE"

        /** The remote, told to connect to the bridge ("pairing_ip_address"). */
        fun remoteIntent(context: Context): Intent {
            val host = Prefs(context).bridgeUrl?.toHttpUrlOrNull()?.host
            return Intent(GOOGLE_TV_REMOTE_ACTION)
                .setPackage(GOOGLE_TV_PACKAGE)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
                .putExtra("referrer", context.packageName)
                .apply { if (host != null) putExtra("pairing_ip_address", host) }
        }
    }
}
