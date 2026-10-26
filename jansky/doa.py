"""Direction of arrival from the reSpeaker XVF3800, read over its USB control endpoint.

Same request as Seeed's xvf_host.py DOA_VALUE (resid 20, cmd 18): two uint16s, the angle 0-359 and a
speech flag. A control read takes ~1 ms and works while audio is streaming.
"""
import struct
import threading
import time

import usb.core
import usb.util

from . import config

RESID, CMD = 20, 18
CONTROL_SUCCESS = 0


class Doa:
    def __init__(self, on_change):
        self.on_change = on_change  # called with (angle, speaking)
        self.dev = None
        for pid in config.RESPEAKER_PIDS:
            self.dev = usb.core.find(idVendor=0x2886, idProduct=pid)
            if self.dev:
                break
        self.angle, self.speaking = None, False

    def read(self):
        req = usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE
        for _ in range(20):
            r = self.dev.ctrl_transfer(req, 0, 0x80 | CMD, RESID, 5, 1000)
            if r[0] == CONTROL_SUCCESS:
                angle, speech = struct.unpack("<HH", bytes(r[1:5]))
                return angle, bool(speech)
            time.sleep(0.005)
        return None

    def start(self):
        if self.dev is None:
            print('{"event": "doa_unavailable"}', flush=True)
            return
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                got = self.read()
            except usb.core.USBError:
                got = None
            if got:
                angle, speaking = got
                if speaking != self.speaking or (speaking and angle != self.angle):
                    self.angle, self.speaking = angle, speaking
                    self.on_change(angle, speaking)
            time.sleep(0.1)
