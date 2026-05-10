# BLE HID central for the Social Battery app.
# Scans for any HID advertiser (UUID 0x1812), connects, subscribes to Report
# characteristics, and delivers raw HID usage codes to a callback.
#
# Requires firmware built with MICROPY_PY_BLUETOOTH = 1.
# Author: Yale32  |  youtube.com/@yale32

import struct

# BLE IRQ event codes
_IRQ_SCAN_RESULT               = 5
_IRQ_SCAN_DONE                 = 6
_IRQ_PERIPHERAL_CONNECT        = 7
_IRQ_PERIPHERAL_DISCONNECT     = 8
_IRQ_GATTC_SERVICE_RESULT      = 9
_IRQ_GATTC_SERVICE_DONE        = 10
_IRQ_GATTC_CHARACTERISTIC_RESULT = 11
_IRQ_GATTC_CHARACTERISTIC_DONE   = 12
_IRQ_GATTC_WRITE_DONE          = 17
_IRQ_GATTC_NOTIFY              = 18
_IRQ_GATTC_INDICATE            = 19

_PROP_NOTIFY    = 0x10
_PROP_INDICATE  = 0x20


def _adv_has_hid(adv_data):
    """Return True if UUID 0x1812 (HID) appears in a BLE advertisement payload."""
    i = 0
    while i < len(adv_data):
        length = adv_data[i]
        if length == 0 or i + length >= len(adv_data):
            break
        ad_type = adv_data[i + 1]
        if ad_type in (0x02, 0x03):   # 16-bit UUID list (incomplete / complete)
            for j in range(i + 2, i + 1 + length, 2):
                if j + 1 < len(adv_data):
                    if struct.unpack_from('<H', adv_data, j)[0] == 0x1812:
                        return True
        i += 1 + length
    return False


class BLERemote:
    """
    BLE central for the Jetion Remote V3.0 HID peripheral.

    Scans indefinitely for any BLE HID advertiser (UUID 0x1812), connects,
    enables notifications on all HID Report characteristics by writing directly
    to val_h+1 (the CCCD always lives immediately after the value in HID servers),
    then delivers raw HID bytes to on_key(buf).

    on_key fires from the BLE IRQ context (between MicroPython bytecodes).
    """

    def __init__(self, on_key):
        import bluetooth

        self._on_key          = on_key
        self._conn            = None
        self._svc_start       = 0
        self._svc_end         = 0
        self._notify_val_hdls = []   # value handles of all notify/indicate chars
        self._proto_mode_hdl  = None # value handle for HID Protocol Mode (0x2A4E)
        self._cccd_queue      = []   # (cccd_handle, value) pairs pending write

        self.connected = False
        self.scanning  = False

        self._UUID_HID        = bluetooth.UUID(0x1812)
        self._UUID_REPORT     = bluetooth.UUID(0x2A4D)
        self._UUID_PROTO_MODE = bluetooth.UUID(0x2A4E)

        self._ble = bluetooth.BLE()
        self._ble.active(True)
        try:
            self._ble.config(bond=True, mitm=False, io=3)
        except BaseException:
            pass
        self._ble.irq(self._irq)
        self._start_scan()

    # ── scanning ────────────────────────────────────────────────────────────────

    def _start_scan(self):
        self.connected = False
        self.scanning  = True
        self._conn     = None
        try:
            self._ble.gap_scan(0, 30000, 30000, True)
        except TypeError:
            self._ble.gap_scan(0, 30000, 30000)

    # ── logging ─────────────────────────────────────────────────────────────────

    def _log(self, msg):
        try:
            with open("/ble_debug.txt", "a") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    # ── IRQ handler ─────────────────────────────────────────────────────────────

    def _irq(self, event, data):
        if event == _IRQ_SCAN_RESULT:
            addr_type, addr, adv_type, rssi, adv_data = data
            if _adv_has_hid(bytes(adv_data)):
                self._ble.gap_scan(None)
                self.scanning = False
                self._ble.gap_connect(addr_type, addr)

        elif event == _IRQ_SCAN_DONE:
            if self.scanning:
                self._start_scan()

        elif event == _IRQ_PERIPHERAL_CONNECT:
            conn_handle, addr_type, addr = data
            self._conn            = conn_handle
            self._notify_val_hdls = []
            self._proto_mode_hdl  = None
            self._cccd_queue      = []
            self._log("connected")
            self._ble.gattc_discover_services(conn_handle)

        elif event == _IRQ_PERIPHERAL_DISCONNECT:
            self._conn     = None
            self.connected = False
            self._log("disconnected")
            self._start_scan()

        elif event == _IRQ_GATTC_SERVICE_RESULT:
            conn_handle, start_handle, end_handle, uuid = data
            if uuid == self._UUID_HID:
                self._svc_start = start_handle
                self._svc_end   = end_handle
                self._log("HID svc " + str(start_handle) + "-" + str(end_handle))

        elif event == _IRQ_GATTC_SERVICE_DONE:
            conn_handle, status = data
            if self._svc_end:
                self._ble.gattc_discover_characteristics(
                    conn_handle, self._svc_start, self._svc_end
                )

        elif event == _IRQ_GATTC_CHARACTERISTIC_RESULT:
            conn_handle, _, value_handle, properties, uuid = data
            self._log("char " + str(uuid) + " props=" + hex(properties) + " val_h=" + str(value_handle))
            if uuid == self._UUID_REPORT and (properties & (_PROP_NOTIFY | _PROP_INDICATE)):
                self._notify_val_hdls.append((value_handle, properties))
            elif uuid == self._UUID_PROTO_MODE:
                self._proto_mode_hdl = value_handle

        elif event == _IRQ_GATTC_CHARACTERISTIC_DONE:
            conn_handle, status = data
            # CCCD always sits at val_h+1 in a well-formed HID GATT server.
            # We skip descriptor-range discovery because the Jetion's GATT handle
            # ranges span across characteristics, causing missed CCCDs. Writing
            # directly to val_h+1 is simpler and more reliable.
            for val_h, props in self._notify_val_hdls:
                cccd_val = b'\x01\x00' if (props & _PROP_NOTIFY) else b'\x02\x00'
                self._cccd_queue.append((val_h + 1, cccd_val))
            # Protocol Mode (0x2A4E) = 0x01 forces Report Protocol so the remote
            # sends structured HID Report notifications instead of Boot Protocol.
            if self._proto_mode_hdl is not None:
                try:
                    self._ble.gattc_write(conn_handle, self._proto_mode_hdl, b'\x01', 0)
                    self._log("set proto mode Report on h=" + str(self._proto_mode_hdl))
                except BaseException as e:
                    self._log("proto mode err " + str(e))
            self._log("queued " + str(len(self._cccd_queue)) + " CCCDs")
            self._write_next_cccd(conn_handle)

        elif event == _IRQ_GATTC_WRITE_DONE:
            conn_handle, value_handle, status = data
            self._log("write done h=" + str(value_handle) + " st=" + str(status))
            self._write_next_cccd(conn_handle)

        elif event in (_IRQ_GATTC_NOTIFY, _IRQ_GATTC_INDICATE):
            conn_handle, value_handle, notify_data = data
            buf = bytes(notify_data)
            self._log("notify h=" + str(value_handle) + " " + buf.hex())
            if buf:
                # Pass buf[0] to the callback for every report including 0x00
                # (key-up). The app layer uses 0x00 to clear its debounce state
                # so it can detect the next distinct physical press.
                self._on_key(buf[0])

        else:
            self._log("IRQ " + str(event))

    # ── CCCD sequential write ───────────────────────────────────────────────────

    def _write_next_cccd(self, conn_handle):
        # CCCDs must be written one at a time. Issuing all writes simultaneously
        # overflows the ESP32-S3 BLE stack's internal queue and returns ENOMEM.
        # Each write completes with IRQ 17 (_IRQ_GATTC_WRITE_DONE), which calls
        # this method again to issue the next one.
        if not self._cccd_queue:
            self._log("all CCCDs written")
            self.connected = True
            return
        cccd_h, cccd_val = self._cccd_queue.pop(0)
        try:
            self._ble.gattc_write(conn_handle, cccd_h, cccd_val, 1)
            self._log("writing CCCD h=" + str(cccd_h) + " val=" + cccd_val.hex())
        except BaseException as e:
            self._log("CCCD err h=" + str(cccd_h) + " " + str(e))
            self._write_next_cccd(conn_handle)  # skip failed handle and try next

    # ── public ──────────────────────────────────────────────────────────────────

    def close(self):
        try:
            self._ble.gap_scan(None)
        except BaseException:
            pass
        if self._conn is not None:
            try:
                self._ble.gap_disconnect(self._conn)
            except BaseException:
                pass
        # Intentionally not calling self._ble.active(False) — deactivating the
        # BLE radio while USB is also active causes a hard hang on ESP32-S3.
