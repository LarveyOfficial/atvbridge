package com.atvbridge.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/** Restarts the bridge service after a reboot or an app update, if it was running before. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val prefs = Prefs(context)
        if (!prefs.enabled || prefs.bridgeUrl == null) return
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED -> if (!prefs.startOnBoot) return
            Intent.ACTION_MY_PACKAGE_REPLACED -> Unit
            else -> return
        }
        try {
            BridgeService.start(context)
        } catch (e: Exception) {
            Log.w("BootReceiver", "Could not start bridge service", e)
        }
    }
}
