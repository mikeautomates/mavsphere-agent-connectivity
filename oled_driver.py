"""Minimal I2C driver for SSD1306 / SSD1309 128x64 monochrome OLEDs (needs python3-smbus)."""

W, H = 128, 64


class SSD1306:
    def __init__(self, bus, addr, rotate=False, controller="ssd1306"):
        import smbus  # imported here so renderers can be tested without hardware
        self.bus = smbus.SMBus(bus)
        self.addr = addr
        seg, com = (0xA0, 0xC0) if rotate else (0xA1, 0xC8)
        init = [0xAE, 0xD5, 0x80, 0xA8, 0x3F, 0xD3, 0x00, 0x40]
        if controller != "ssd1309":      # SSD1309 has no internal charge pump
            init += [0x8D, 0x14]
        init += [0x20, 0x00, seg, com, 0xDA, 0x12, 0x81, 0xCF, 0xD9, 0xF1,
                 0xDB, 0x40, 0xA4, 0xA6, 0xAF]
        for c in init:
            self.cmd(c)

    def cmd(self, c):
        self.bus.write_byte_data(self.addr, 0x00, c)

    def show(self, img):
        px = img.convert("1").load()
        buf = []
        for page in range(H // 8):
            for x in range(W):
                b = 0
                for bit in range(8):
                    if px[x, page * 8 + bit]:
                        b |= 1 << bit
                buf.append(b)
        for c in (0x21, 0, W - 1, 0x22, 0, 7):
            self.cmd(c)
        for i in range(0, len(buf), 16):
            self.bus.write_i2c_block_data(self.addr, 0x40, buf[i:i + 16])
