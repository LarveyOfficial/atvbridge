package com.larvey.atvbridge

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast

class MainActivity : Activity() {

    private lateinit var prefs: Prefs
    private lateinit var discovery: Discovery
    private lateinit var status: TextView
    private lateinit var toggle: Button
    private lateinit var url: EditText
    private lateinit var found: LinearLayout
    private lateinit var battery: Button
    private val foundUrls = mutableSetOf<String>()
    private val statusListener: () -> Unit = { render() }

    @SuppressLint("UseSwitchCompatOrMaterialCode")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        prefs = Prefs(this)
        discovery = Discovery(this, ::onBridgeFound)

        status = findViewById(R.id.status)
        toggle = findViewById(R.id.toggle)
        url = findViewById(R.id.url)
        found = findViewById(R.id.found)
        battery = findViewById(R.id.battery)

        url.setText(prefs.bridgeUrl ?: "")
        url.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_DONE) {
                url.clearFocus()
                saveUrl()
            }
            false
        }
        toggle.setOnClickListener {
            if (BridgeService.isRunning) {
                prefs.enabled = false
                BridgeService.stop(this)
            } else {
                startBridge()
            }
        }
        findViewById<Button>(R.id.find).setOnClickListener { search() }
        findViewById<Button>(R.id.dashboard).setOnClickListener {
            val base = Prefs.normalizeUrl(url.text.toString())
            if (base == null) toast(R.string.invalid_url)
            else startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(base)))
        }

        findViewById<Switch>(R.id.volume_keys).apply {
            isChecked = prefs.volumeKeys
            setOnCheckedChangeListener { _, checked -> prefs.volumeKeys = checked }
        }
        findViewById<Switch>(R.id.start_on_boot).apply {
            isChecked = prefs.startOnBoot
            setOnCheckedChangeListener { _, checked -> prefs.startOnBoot = checked }
        }
        findViewById<Button>(R.id.licenses).setOnClickListener {
            val text = assets.open("licenses.txt").bufferedReader().use { it.readText() }
            AlertDialog.Builder(this)
                .setTitle(R.string.licenses)
                .setMessage(text)
                .setPositiveButton(android.R.string.ok, null)
                .show()
        }
        // Android requires an ongoing notification to stay connected in the background; its
        // channel can be switched off without stopping the connection.
        findViewById<Button>(R.id.hide_status).setOnClickListener {
            BridgeService.createChannels(this)
            startActivity(
                Intent(Settings.ACTION_CHANNEL_NOTIFICATION_SETTINGS)
                    .putExtra(Settings.EXTRA_APP_PACKAGE, packageName)
                    .putExtra(Settings.EXTRA_CHANNEL_ID, BridgeService.CHANNEL_STATUS),
            )
        }
        battery.setOnClickListener {
            @SuppressLint("BatteryLife")
            val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName"))
            runCatching { startActivity(intent) }
        }
    }

    override fun onResume() {
        super.onResume()
        BridgeService.statusListeners.add(statusListener)
        render()
        val power = getSystemService(PowerManager::class.java)
        battery.visibility = if (power.isIgnoringBatteryOptimizations(packageName)) View.GONE else View.VISIBLE
        if (prefs.bridgeUrl == null) search()
    }

    override fun onPause() {
        BridgeService.statusListeners.remove(statusListener)
        discovery.stop()
        super.onPause()
    }

    private fun render() {
        status.text = BridgeService.status ?: getString(R.string.status_not_running)
        toggle.setText(if (BridgeService.isRunning) R.string.stop else R.string.start)
    }

    /** Returns true if the address changed. */
    private fun saveUrl(): Boolean {
        val normalized = Prefs.normalizeUrl(url.text.toString()) ?: return false
        url.setText(normalized)
        val changed = normalized != prefs.bridgeUrl
        prefs.bridgeUrl = normalized
        if (changed && BridgeService.isRunning) BridgeService.reconnect(this)
        return changed
    }

    private fun startBridge() {
        if (Prefs.normalizeUrl(url.text.toString()) == null) {
            toast(R.string.invalid_url)
            return
        }
        saveUrl()
        prefs.enabled = true
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        BridgeService.start(this)
    }

    private fun search() {
        found.removeAllViews()
        foundUrls.clear()
        val hint = TextView(this).apply {
            text = getString(R.string.searching)
            alpha = 0.7f
        }
        found.addView(hint)
        discovery.stop()
        discovery.start()
    }

    private fun onBridgeFound(name: String, bridgeUrl: String) {
        if (!foundUrls.add(bridgeUrl)) return
        if (url.text.isBlank()) url.setText(bridgeUrl)
        found.addView(Button(this, null, 0, android.R.style.Widget_Material_Button_Borderless_Colored).apply {
            text = "$name — ${bridgeUrl.removePrefix("http://")}"
            isAllCaps = false
            textAlignment = View.TEXT_ALIGNMENT_VIEW_START
            setOnClickListener {
                url.setText(bridgeUrl)
                saveUrl()
            }
        })
    }

    private fun toast(res: Int) = Toast.makeText(this, res, Toast.LENGTH_LONG).show()
}
