package com.atvbridge.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PrefsTest {
    @Test
    fun normalizesBridgeAddresses() {
        assertEquals("http://192.168.0.10:8090", Prefs.normalizeUrl("192.168.0.10"))
        assertEquals("http://192.168.0.10:8090", Prefs.normalizeUrl(" 192.168.0.10:8090/ "))
        assertEquals("http://192.168.0.10:9000", Prefs.normalizeUrl("192.168.0.10:9000"))
        assertEquals("http://tower.local:8090", Prefs.normalizeUrl("http://tower.local"))
        // Explicit default port is kept (and printed without ":80").
        assertEquals("http://tower.local", Prefs.normalizeUrl("http://tower.local:80/"))
        assertEquals("http://[fe80::1]:8090", Prefs.normalizeUrl("[fe80::1]"))
        assertEquals("http://[fe80::1]:8091", Prefs.normalizeUrl("http://[fe80::1]:8091"))
    }

    @Test
    fun rejectsGarbage() {
        assertNull(Prefs.normalizeUrl(""))
        assertNull(Prefs.normalizeUrl("   "))
        assertNull(Prefs.normalizeUrl("ftp://x"))
    }
}
