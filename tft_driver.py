"""ST7735 (1.8" 128x160 SPI colour TFT) driver using spidev + lgpio.

Needs python3-spidev and python3-lgpio (apt) and SPI enabled (dtparam=spi=on).
The spi/gpio objects can be injected, which is how the tests run it.
"""
import time

# Adafruit's init sequence for the common "green tab" 1.8" ST7735 / ST7735S.
CMD_SWRESET, CMD_SLPOUT, CMD_NORON, CMD_INVOFF, CMD_INVON = 0x01, 0x11, 0x13, 0x20, 0x21
CMD_DISPON, CMD_CASET, CMD_RASET, CMD_RAMWR = 0x29, 0x2A, 0x2B, 0x2C
CMD_MADCTL, CMD_COLMOD = 0x36, 0x3A

INIT = [
    (0xB1, [0x01, 0x2C, 0x2D]),
    (0xB2, [0x01, 0x2C, 0x2D]),
    (0xB3, [0x01, 0x2C, 0x2D, 0x01, 0x2C, 0x2D]),
    (0xB4, [0x07]),
    (0xC0, [0xA2, 0x02, 0x84]),
    (0xC1, [0xC5]),
    (0xC2, [0x0A, 0x00]),
    (0xC3, [0x8A, 0x2A]),
    (0xC4, [0x8A, 0xEE]),
    (0xC5, [0x0E]),
    (CMD_COLMOD, [0x05]),                       # 16 bit colour
    (0xE0, [0x02, 0x1C, 0x07, 0x12, 0x37, 0x32, 0x29, 0x2D, 0x29, 0x25, 0x2B, 0x39, 0x00, 0x01, 0x03, 0x10]),
    (0xE1, [0x03, 0x1D, 0x07, 0x06, 0x2E, 0x2C, 0x29, 0x2D, 0x2E, 0x2E, 0x37, 0x3F, 0x00, 0x00, 0x02, 0x10]),
]
MADCTL = {0: 0xC0, 90: 0xA0, 180: 0x00, 270: 0x60}
CHUNK = 4096


class _LgpioPins:
    """Tiny wrapper so the driver only needs write(pin, level)."""

    def __init__(self, pins):
        import lgpio
        self.lg = lgpio
        self.h = None
        for chip in (0, 4):                     # Pi 4 and earlier: gpiochip0; Pi 5: gpiochip4
            try:
                self.h = lgpio.gpiochip_open(chip)
                break
            except Exception:
                continue
        if self.h is None:
            raise OSError("cannot open a gpiochip (is python3-lgpio installed?)")
        for p in pins:
            lgpio.gpio_claim_output(self.h, p, 0)

    def write(self, pin, level):
        self.lg.gpio_write(self.h, pin, 1 if level else 0)

    def close(self):
        self.lg.gpiochip_close(self.h)


class ST7735:
    def __init__(self, spi_bus=0, spi_device=0, speed_hz=16000000, dc_pin=25, rst_pin=24,
                 blk_pin=None, width=128, height=160, rotation=90, col_offset=0, row_offset=0,
                 bgr=False, invert=False, spi=None, gpio=None):
        if rotation not in MADCTL:
            raise ValueError("rotation must be 0, 90, 180 or 270")
        self.dc, self.rst, self.blk = dc_pin, rst_pin, blk_pin
        self.rotation, self.bgr, self.invert = rotation, bgr, invert
        landscape = rotation in (90, 270)
        self.width = height if landscape else width
        self.height = width if landscape else height
        # Adafruit behaviour: offsets are given for portrait and swapped for landscape.
        self.xoff, self.yoff = (row_offset, col_offset) if landscape else (col_offset, row_offset)
        if spi is None:
            import spidev
            spi = spidev.SpiDev()
            spi.open(spi_bus, spi_device)
            spi.max_speed_hz = speed_hz
            spi.mode = 0
        self.spi = spi
        pins = [p for p in (dc_pin, rst_pin, blk_pin) if p is not None]
        self.gpio = gpio if gpio is not None else _LgpioPins(pins)
        self._init()

    # ---- low level ---------------------------------------------------
    def _write(self, data):
        data = bytes(data)
        if not data:
            return
        w2 = getattr(self.spi, "writebytes2", None)
        if w2 is not None:
            w2(data)
            return
        for i in range(0, len(data), CHUNK):
            self.spi.writebytes(list(data[i:i + CHUNK]))

    def _cmd(self, cmd, data=()):
        self.gpio.write(self.dc, 0)
        self._write([cmd])
        if data:
            self.gpio.write(self.dc, 1)
            self._write(data)

    def _init(self):
        self.gpio.write(self.rst, 1)
        time.sleep(0.02)
        self.gpio.write(self.rst, 0)
        time.sleep(0.05)
        self.gpio.write(self.rst, 1)
        time.sleep(0.15)
        self._cmd(CMD_SWRESET)
        time.sleep(0.15)
        self._cmd(CMD_SLPOUT)
        time.sleep(0.5)
        for cmd, data in INIT:
            self._cmd(cmd, data)
        self._cmd(CMD_MADCTL, [MADCTL[self.rotation] | (0x00 if self.bgr else 0x08)])
        self._cmd(CMD_INVON if self.invert else CMD_INVOFF)
        self._cmd(CMD_NORON)
        time.sleep(0.01)
        self._cmd(CMD_DISPON)
        time.sleep(0.1)
        if self.blk is not None:
            self.gpio.write(self.blk, 1)

    def _window(self, x0, y0, x1, y1):
        x0, x1 = x0 + self.xoff, x1 + self.xoff
        y0, y1 = y0 + self.yoff, y1 + self.yoff
        self._cmd(CMD_CASET, [x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF])
        self._cmd(CMD_RASET, [y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF])
        self._cmd(CMD_RAMWR)

    # ---- public ------------------------------------------------------
    @staticmethod
    def rgb565(img):
        data = img.convert("RGB").tobytes()
        out = bytearray(len(data) // 3 * 2)
        j = 0
        for i in range(0, len(data), 3):
            v = ((data[i] & 0xF8) << 8) | ((data[i + 1] & 0xFC) << 3) | (data[i + 2] >> 3)
            out[j] = v >> 8
            out[j + 1] = v & 0xFF
            j += 2
        return bytes(out)

    def show(self, img):
        if img.size != (self.width, self.height):
            raise ValueError(f"image is {img.size}, display is {(self.width, self.height)}")
        self._window(0, 0, self.width - 1, self.height - 1)
        self.gpio.write(self.dc, 1)
        self._write(self.rgb565(img))

    def backlight(self, on):
        if self.blk is not None:
            self.gpio.write(self.blk, 1 if on else 0)
