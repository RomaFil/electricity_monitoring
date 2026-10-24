#!/usr/bin/env python3
"""Pictures of the LCD 1602 screens, without hardware.

The screens are a port of compose() / bigClock() / weather() / forecastScreen() / startup() from
firmware/src/display.cpp. The custom characters (GLYPHS) and weather icons (ICONS) are read from
that file itself, so a changed glyph shows up here at once. Letters come from the HD44780 A00 ROM
(the usual PCF8574 backpack LCD). Needs Pillow.

  python tools/lcd_preview.py              # PNGs + GIF into docs/lcd/
  python tools/lcd_preview.py --theme green
"""
import argparse
import math
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
DISPLAY_CPP = ROOT / "firmware" / "src" / "display.cpp"


def cgram_tables():
    """GLYPHS[6][8] and ICONS[8][8] byte tables from display.cpp."""
    src = DISPLAY_CPP.read_text(encoding="utf-8")

    def table(name):
        m = re.search(name + r"\[\d+\]\[8\] = \{(.*?)\n\s*\};", src, re.S)
        rows = re.findall(r"\{([^{}]*)\}", m.group(1))
        return [[int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]+", r)] for r in rows]
    return table("GLYPHS"), table("ICONS")


GLYPHS, ICONS = cgram_tables()
# slots 0..5 = GLYPHS (codes 8..13), slots 6 and 7 = icons for "now" and "tomorrow" (codes 14, 15)
cgram = GLYPHS + [ICONS[3], ICONS[5]]

# HD44780 A00 ROM, the characters the firmware prints. 7 rows; the 8th (cursor row) is empty.
ROM = {
    " ": ["00000"] * 7,
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    ":": ["00000", "01100", "01100", "00000", "01100", "01100", "00000"],
    "/": ["00000", "00001", "00010", "00100", "01000", "10000", "00000"],
    "%": ["11000", "11001", "00010", "00100", "01000", "10011", "00011"],
    "\xdf": ["11100", "10100", "11100", "00000", "00000", "00000", "00000"],  # degree sign
    "A": ["01110", "10001", "10001", "10001", "11111", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "N": ["10001", "10001", "11001", "10101", "10011", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "a": ["00000", "00000", "01110", "00001", "01111", "10001", "01111"],
    "b": ["10000", "10000", "10110", "11001", "10001", "10001", "11110"],
    "c": ["00000", "00000", "01110", "10000", "10000", "10001", "01110"],
    "d": ["00001", "00001", "01101", "10011", "10001", "10001", "01111"],
    "e": ["00000", "00000", "01110", "10001", "11111", "10000", "01110"],
    "f": ["00110", "01001", "01000", "11100", "01000", "01000", "01000"],
    "h": ["10000", "10000", "10110", "11001", "10001", "10001", "10001"],
    "i": ["00100", "00000", "01100", "00100", "00100", "00100", "01110"],
    "k": ["10000", "10000", "10010", "10100", "11000", "10100", "10010"],
    "l": ["01100", "00100", "00100", "00100", "00100", "00100", "01110"],
    "n": ["00000", "00000", "10110", "11001", "10001", "10001", "10001"],
    "o": ["00000", "00000", "01110", "10001", "10001", "10001", "01110"],
    "p": ["00000", "00000", "11110", "10001", "11110", "10000", "10000"],
    "r": ["00000", "00000", "10110", "11001", "10000", "10000", "10000"],
    "s": ["00000", "00000", "01110", "10000", "01110", "00001", "11110"],
    "t": ["01000", "01000", "11100", "01000", "01000", "01001", "00110"],
    "u": ["00000", "00000", "10001", "10001", "10001", "10011", "01101"],
    "v": ["00000", "00000", "10001", "10001", "10001", "01010", "00100"],
    "w": ["00000", "00000", "10001", "10001", "10101", "10101", "01010"],
    "y": ["00000", "00000", "10001", "10001", "01111", "00001", "01110"],
}


def bitmap(ch):
    code = ord(ch)
    if 8 <= code <= 15:
        return cgram[code - 8]
    if code == 0xFF:
        return [0x1F] * 8  # full block
    if ch not in ROM:
        raise KeyError(f"no ROM bitmap for {ch!r}; add it to ROM")
    return [int(r, 2) for r in ROM[ch]] + [0]


# ---------- screens: port of display.cpp ----------

WIFI_DOT, TOP, BOT, BOTH, COLON_UP, COLON_DN, ICON_NOW, ICON_TMR = (chr(c) for c in range(8, 16))
FULL, DEG = "\xff", "\xdf"
SKY = ["Clear", "ClearNight", "Partly", "Cloudy", "Fog", "Rain", "Snow", "Storm"]
BIG = [
    [FULL + TOP + FULL, FULL + BOT + FULL],  # 0
    [TOP + FULL + " ", BOT + FULL + BOT],    # 1
    [BOTH + BOTH + FULL, FULL + BOT + BOT],  # 2
    [BOTH + BOTH + FULL, BOT + BOT + FULL],  # 3
    [FULL + BOT + FULL, "  " + FULL],        # 4
    [FULL + BOTH + BOTH, BOT + BOT + FULL],  # 5
    [FULL + BOTH + BOTH, FULL + BOT + FULL], # 6
    [TOP + TOP + FULL, "  " + FULL],         # 7
    [FULL + BOTH + FULL, FULL + BOT + FULL], # 8
    [FULL + BOTH + FULL, BOT + BOT + FULL],  # 9
    [BOT + BOT + BOT, "   "],                # 10: dash, time unknown
]


def big_clock(hh=None, mm=None, ss=0):
    d = [10] * 4 if hh is None else [hh // 10, hh % 10, mm // 10, mm % 10]
    colon = True if hh is None else ss % 2 == 0
    lines = []
    for row in range(2):
        s = ""
        for i in range(4):
            s += "   " if i == 0 and d[0] == 0 else BIG[d[i]][row]
            if i == 1:
                s += (COLON_DN if row else COLON_UP) if colon else " "
            elif i != 3:
                s += " "
        lines.append(s + " ")  # no Wi-Fi dot next to the big digits
    return lines


def weather(t, h=None, p=None, wifi=True):
    l1 = f"{t:.1f}" + DEG + "C"
    if h is not None:
        l1 = l1.ljust(12) + f"{h:3.0f}%"
    l2 = f"{p:.0f} hPa" if p is not None else ""
    return [l1, l2.ljust(15) + (WIFI_DOT if wifi else " ")]


def forecast(now_t, now_sky, tmr_min, tmr_max, tmr_sky, rain=-1, name="Babyntsi"):
    cgram[6], cgram[7] = ICONS[SKY.index(now_sky)], ICONS[SKY.index(tmr_sky)]  # loadIcon()
    right = ICON_NOW + " " + f"{now_t:.0f}" + DEG
    l1 = name[:15 - len(right)]
    l1 = l1 + " " * (16 - len(l1) - len(right)) + right
    temps = f"{tmr_min:.0f}/{tmr_max:.0f}" + DEG
    l2 = "Zavtra" + ICON_TMR + " " + temps
    if rain >= 20:
        pct = f"{rain}%"
        if len(l2) + 1 + len(pct) > 16:
            l2 = "Zavtra" + ICON_TMR + temps  # no space after the icon when it does not fit
        if len(l2) + 1 + len(pct) <= 16:
            l2 = l2.ljust(16 - len(pct)) + pct
    return [l1, l2]


def startup(msg, hms=None):
    return ["%02d:%02d:%02d" % hms if hms else "--:--:--", msg]


def fit(line):
    return (line + " " * 16)[:16]  # writeLine(): pad to 16 and cut


# ---------- drawing ----------

THEMES = {
    # backlight, lit dot, unlit dot (unlit dots are faintly visible on a real LCD)
    "blue": ((24, 64, 214), (232, 240, 255), (40, 82, 222)),
    "green": ((150, 196, 40), (22, 38, 12), (140, 184, 40)),
    "off": ((14, 22, 46), (14, 22, 46), (17, 26, 52)),  # monitoring mode: display and backlight off
}
DOT, GAP, CHAR_GAP, ROW_GAP, PAD = 6, 1, 6, 8, 26


def caption_font():
    for name in ("DejaVuSans.ttf", "segoeui.ttf", "arial.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, 17)
        except OSError:
            pass
    return ImageFont.load_default()


def render(lines, theme="blue", caption=None):
    bg, on, off = THEMES[theme]
    pitch = DOT + GAP
    cw, ch = 5 * pitch - GAP, 8 * pitch - GAP
    w = PAD * 2 + 16 * cw + 15 * CHAR_GAP
    h = PAD * 2 + 2 * ch + ROW_GAP
    cap_h = 34 if caption else 0
    img = Image.new("RGB", (w + 24, h + 24 + cap_h), (18, 18, 20))
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle((4, 4, w + 19, h + 19), 10, fill=(10, 10, 12))  # bezel
    dr.rounded_rectangle((12, 12, w + 11, h + 11), 4, fill=bg)          # glass
    for r, line in enumerate(lines):
        for c, chv in enumerate(fit(line)):
            x0, y0 = 12 + PAD + c * (cw + CHAR_GAP), 12 + PAD + r * (ch + ROW_GAP)
            for y, bits in enumerate(bitmap(chv)):
                for x in range(5):
                    px, py = x0 + x * pitch, y0 + y * pitch
                    dr.rectangle((px, py, px + DOT - 1, py + DOT - 1), fill=on if bits >> (4 - x) & 1 else off)
    if caption:
        dr.text((14, h + 30), caption, fill=(200, 200, 208), font=caption_font())
    return img


def screens(fw):
    # forecast() loads the icons into CGRAM slots 6/7 like loadIcon(), so each screen is built lazily
    return [
        ("01-power-on", lambda: ["Svitlo-Local", "fw " + fw], "Увімкнення"),
        ("02-wifi-connecting", lambda: startup("WiFi..."), "Перші 30 с: шукає Wi-Fi, часу ще нема"),
        ("03-wifi-ok", lambda: startup("WiFi OK", (14, 7, 32)), "Wi-Fi є, NTP відповів (5 с)"),
        ("04-wifi-no-network", lambda: startup("WiFi: no network"), "30 с без мережі (5 с)"),
        ("05-setup-ap", lambda: startup("AP SVITLO-LOCAL"), "Перше налаштування: точка SVITLO-LOCAL"),
        ("06-clock", lambda: big_clock(14, 7, 0), "Годинник, 9 с з кожних 15"),
        ("07-clock-colon-off", lambda: big_clock(14, 7, 1), "Двокрапка блимає раз на секунду"),
        ("08-clock-morning", lambda: big_clock(8, 5, 0), "« 8:05» — без нуля попереду"),
        ("09-clock-unknown", lambda: big_clock(None), "Часу ще нема (без DS3231 до відповіді NTP)"),
        ("10-room", lambda: weather(21.4, 58, 1001), "У кімнаті (BME280), 3 с; крапка — Wi-Fi є"),
        ("11-room-no-wifi", lambda: weather(21.4, 58, 1001, wifi=False), "Те саме без Wi-Fi: крапки нема"),
        ("12-forecast-rain", lambda: forecast(11, "Cloudy", 6, 14, "Rain", 80), "Прогноз Open-Meteo, 3 с: завтра дощ 80 %"),
        ("13-forecast-sun", lambda: forecast(18, "Clear", 9, 21, "Partly"), "Прогноз: сонце, завтра мінлива хмарність"),
        ("14-forecast-snow", lambda: forecast(-3, "Snow", -8, -1, "Storm", 40), "Прогноз узимку: сніг, завтра гроза"),
        ("15-no-wifi-ap", lambda: startup("No WiFi: AP on", (14, 7, 44)), "Мережі нема, увімкнено точку доступу"),
        ("16-reset-countdown", lambda: startup("Reset in 7 s", (14, 7, 50)), "Утримується FLASH: скидання через 7 с"),
        ("17-ota-start", lambda: startup("fw 0.7.1..."), "Віддалене оновлення: підключення до GitHub"),
        ("18-ota-progress", lambda: startup("fw update 45%"), "Завантаження й запис підписаного образу"),
        ("19-monitoring", lambda: ["", ""], "Режим моніторингу: дисплей і підсвітка вимкнені"),
    ]


def cycle_gif(out, theme):
    """One 15 s cycle of compose() with a forecast: 9 s clock, 3 s room, 3 s forecast."""
    frames = []
    for sec in range(15):
        if sec < 9:
            lines = big_clock(14, 7, sec)
        elif sec < 12:
            lines = weather(21.4, 58, 1001)
        else:
            lines = forecast(11, "Cloudy", 6, 14, "Rain", 80)
        frames.append(render(lines, theme, f"цикл 15 с, секунда {sec}"))
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=1000, loop=0, optimize=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "lcd"))
    ap.add_argument("--theme", choices=("blue", "green"), default="blue")
    a = ap.parse_args()
    fw = re.search(r'FW_VERSION=\\"([^"\\]+)', (ROOT / "firmware" / "platformio.ini").read_text()).group(1)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tiles = []
    for name, make, caption in screens(fw):
        img = render(make(), "off" if name.endswith("monitoring") else a.theme, caption)
        img.save(out / f"{name}.png", optimize=True)
        tiles.append(img)
    tw, th = tiles[0].size
    cols = 2
    sheet = Image.new("RGB", (cols * tw, math.ceil(len(tiles) / cols) * th), (18, 18, 20))
    for i, tile in enumerate(tiles):
        sheet.paste(tile, ((i % cols) * tw, (i // cols) * th))
    sheet.save(out / "all-screens.png", optimize=True)
    cycle_gif(out / "cycle.gif", a.theme)
    print(f"{len(tiles)} screens, all-screens.png and cycle.gif in {out}")


if __name__ == "__main__":
    main()
