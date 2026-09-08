from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


BADGE_SHEET = Path(r"C:\Users\saket\Downloads\ChatGPT Image Aug 21, 2026, 07_33_52 PM.png")
JERSEY_SHEET = Path(r"C:\Users\saket\Downloads\ChatGPT Image Aug 21, 2026, 07_16_00 PM.png")
BASE = Path(__file__).resolve().parents[1] / "public" / "assets" / "premier-league" / "2026-27"

BADGE_GRID = [
    ["arsenal", "liverpool", "manchester-city", "chelsea", "manchester-united"],
    ["tottenham-hotspur", "newcastle-united", "aston-villa", "brighton", None],
    [None, None, "everton", "bournemouth", None],
    ["nottingham-forest", "fulham", "brentford", "crystal-palace", "ipswich-town"],
]

FALLBACK_BADGES = {
    "coventry-city": ("COV", "#77c7e8", "#ffffff", "#1b365d"),
    "hull-city": ("HUL", "#f5a800", "#111111", "#ffffff"),
    "leeds-united": ("LEE", "#ffffff", "#1d428a", "#ffcd00"),
    "sunderland": ("SUN", "#eb172b", "#ffffff", "#111111"),
}

TEAMS = [
    ("arsenal", "#db0007", "#ffffff"),
    ("aston-villa", "#95bfe5", "#7a003c"),
    ("bournemouth", "#d71920", "#111111"),
    ("brentford", "#e30613", "#ffffff"),
    ("brighton", "#0057b8", "#ffffff"),
    ("chelsea", "#034694", "#ffffff"),
    ("coventry-city", "#77c7e8", "#ffffff"),
    ("crystal-palace", "#1b458f", "#c4122e"),
    ("everton", "#003399", "#ffffff"),
    ("fulham", "#ffffff", "#111111"),
    ("hull-city", "#f5a800", "#111111"),
    ("ipswich-town", "#0057b8", "#ffffff"),
    ("leeds-united", "#ffffff", "#1d428a"),
    ("liverpool", "#c8102e", "#ffffff"),
    ("manchester-city", "#6cabdd", "#ffffff"),
    ("manchester-united", "#da291c", "#ffffff"),
    ("newcastle-united", "#111111", "#ffffff"),
    ("nottingham-forest", "#dd0000", "#ffffff"),
    ("sunderland", "#eb172b", "#ffffff"),
    ("tottenham-hotspur", "#ffffff", "#132257"),
]

STRIPED = {
    "bournemouth",
    "brentford",
    "brighton",
    "crystal-palace",
    "hull-city",
    "newcastle-united",
    "sunderland",
}

STATUS_SLOTS = {
    "captain": (7, 3),
    "vice-captain": (8, 3),
    "injured-doubtful": (9, 3),
}


def transparent_from_checker(image, tolerance=20):
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    for y in range(rgba.height):
      for x in range(rgba.width):
        r, g, b, a = pixels[x, y]
        is_checker = abs(r - g) < 3 and abs(g - b) < 3 and 218 <= r <= 250
        if is_checker:
            pixels[x, y] = (255, 255, 255, 0)
        elif a < 255:
            pixels[x, y] = (r, g, b, a)
    return rgba


def rgb(hex_color):
    value = hex_color.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def darken(color, factor=0.78):
    return tuple(max(0, min(255, int(channel * factor))) for channel in color)


def lighten(color, factor=0.22):
    return tuple(max(0, min(255, int(channel + (255 - channel) * factor))) for channel in color)


def load_font(size, bold=True):
    for name in (
        "arialbd.ttf" if bold else "arial.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def centered(draw, bounds, text, font, fill):
    left, top, right, bottom = bounds
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    draw.text(
        (left + (right - left - width) / 2, top + (bottom - top - height) / 2 - bbox[1]),
        text,
        font=font,
        fill=fill,
    )


def make_fallback_badge(abbr, primary, secondary, accent):
    p = rgb(primary)
    s = rgb(secondary)
    a = rgb(accent)
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.ellipse((18, 18, 238, 238), fill=darken(p, 0.88), outline=(255, 255, 255, 255), width=8)
    draw.ellipse((34, 34, 222, 222), fill=p, outline=a, width=6)
    shield = [(128, 50), (200, 96), (176, 198), (128, 224), (80, 198), (56, 96)]
    draw.polygon(shield, fill=lighten(p, 0.12), outline=s, width=5)
    text_fill = s if secondary != "#ffffff" else darken(p, 0.45)
    centered(draw, (45, 76, 211, 171), abbr, load_font(58), text_fill)
    centered(draw, (54, 172, 202, 204), "2026", load_font(22, False), a if accent != "#ffffff" else s)
    return image


def make_blank_jersey(slug, primary, secondary):
    p = rgb(primary)
    s = rgb(secondary)
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.ellipse((54, 214, 202, 234), fill=(0, 0, 0, 36))
    shirt = [
        (80, 54),
        (106, 38),
        (150, 38),
        (176, 54),
        (224, 82),
        (198, 128),
        (182, 116),
        (182, 226),
        (74, 226),
        (74, 116),
        (58, 128),
        (32, 82),
    ]
    draw.polygon(shirt, fill=p)

    if slug in STRIPED:
        for x in range(76, 181, 28):
            draw.rectangle((x, 58, x + 14, 226), fill=s)
        if slug in {"brentford", "sunderland"}:
            draw.rectangle((104, 58, 132, 226), fill=s)
        if slug == "crystal-palace":
            draw.rectangle((111, 58, 145, 226), fill=rgb("#c4122e"))
    elif slug == "aston-villa":
        draw.polygon([(32, 82), (80, 54), (74, 116), (58, 128)], fill=s)
        draw.polygon([(176, 54), (224, 82), (198, 128), (182, 116)], fill=s)
    else:
        trim = s if primary != "#ffffff" else rgb("#132257")
        draw.line((78, 92, 182, 92), fill=lighten(p, 0.25) if primary != "#ffffff" else (232, 232, 232), width=5)
        draw.line((84, 130, 176, 130), fill=lighten(p, 0.18) if primary != "#ffffff" else (238, 238, 238), width=4)
        draw.line((74, 224, 182, 224), fill=trim, width=5)

    draw.line(shirt + [shirt[0]], fill=darken(p, 0.7), width=4, joint="curve")
    draw.polygon([(106, 38), (128, 67), (150, 38)], fill=s)
    draw.arc((107, 29, 149, 75), 0, 180, fill=darken(s, 0.82), width=4)
    draw.line((38, 84, 61, 123), fill=darken(p, 0.65), width=5)
    draw.line((218, 84, 195, 123), fill=darken(p, 0.65), width=5)
    return image


def flood_remove_background(image, tolerance=42):
    rgba = image.convert("RGBA")
    width, height = rgba.size
    source = rgba.load()
    seen = set()
    stack = []
    for x in range(width):
        stack.extend([(x, 0), (x, height - 1)])
    for y in range(height):
        stack.extend([(0, y), (width - 1, y)])

    def close(a, b):
        return sum(abs(a[i] - b[i]) for i in range(3)) <= tolerance

    while stack:
        x, y = stack.pop()
        if (x, y) in seen:
            continue
        seen.add((x, y))
        current = source[x, y]
        neighbor_colors = []
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height:
                neighbor_colors.append(source[nx, ny])
        if neighbor_colors and not any(close(current, color) for color in neighbor_colors):
            continue
        source[x, y] = (255, 255, 255, 0)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in seen:
                stack.append((nx, ny))

    alpha = rgba.getchannel("A").filter(ImageFilter.GaussianBlur(0.45))
    rgba.putalpha(alpha)
    return rgba


def contain_square(image, size=256, padding=12):
    bbox = image.getbbox()
    if bbox:
        image = image.crop(bbox)
    scale = min((size - padding * 2) / image.width, (size - padding * 2) / image.height)
    resized = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    canvas.alpha_composite(resized, ((size - resized.width) // 2, (size - resized.height) // 2))
    return canvas


def blank_number_area(image, striped=False):
    rgba = image.convert("RGBA")
    width, height = rgba.size
    body_box = (round(width * 0.21), round(height * 0.24), round(width * 0.81), round(height * 0.83))
    mask = Image.new("L", rgba.size, 0)
    mask_pixels = mask.load()
    source = rgba.load()

    for y in range(body_box[1], body_box[3]):
        for x in range(body_box[0], body_box[2]):
            r, g, b, a = source[x, y]
            if a < 120:
                continue
            brightness = (r + g + b) / 3
            contrast = max(r, g, b) - min(r, g, b)
            is_digit_fill = brightness > 218 or brightness < 46
            is_digit_edge = contrast < 42 and (brightness > 188 or brightness < 74)
            if is_digit_fill or is_digit_edge:
                mask_pixels[x, y] = 255

    mask = mask.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(3))
    softened = rgba.filter(ImageFilter.GaussianBlur(12))
    return Image.composite(softened, rgba, mask)


def crop_badges():
    sheet = Image.open(BADGE_SHEET)
    cell_w = sheet.width / 5
    cell_h = sheet.height / 4
    team_dir = BASE / "teams"
    team_dir.mkdir(parents=True, exist_ok=True)
    for old in team_dir.glob("*.png"):
        old.unlink()

    for row_index, row in enumerate(BADGE_GRID):
        for col_index, slug in enumerate(row):
            if slug is None:
                continue
            left = round(col_index * cell_w + 18)
            upper = round(row_index * cell_h + 4)
            right = round((col_index + 1) * cell_w - 18)
            lower = round((row_index + 1) * cell_h - 4)
            crop = sheet.crop((left, upper, right, lower)).convert("RGBA")
            contain_square(crop, size=256, padding=4).save(team_dir / f"{slug}.png")

    for slug, args in FALLBACK_BADGES.items():
        make_fallback_badge(*args).save(team_dir / f"{slug}.png")


def crop_jerseys():
    jersey_dir = BASE / "jerseys"
    jersey_dir.mkdir(parents=True, exist_ok=True)
    for old in jersey_dir.glob("*.png"):
        old.unlink()

    for slug, primary, secondary in TEAMS:
        make_blank_jersey(slug, primary, secondary).save(jersey_dir / f"{slug}.png")


def crop_status():
    sheet = Image.open(JERSEY_SHEET)
    status_dir = BASE / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    for old in status_dir.glob("*.png"):
        old.unlink()

    cell_w = sheet.width / 10
    row_top = 3 * 218 + 34
    for slug, (col, _row) in STATUS_SLOTS.items():
        left = round(col * cell_w + 28)
        right = round((col + 1) * cell_w - 28)
        crop = transparent_from_checker(sheet.crop((left, row_top, right, row_top + 128)))
        contain_square(crop, size=256, padding=20).save(status_dir / f"{slug}.png")


def main():
    if not BADGE_SHEET.exists():
        raise FileNotFoundError(BADGE_SHEET)
    if not JERSEY_SHEET.exists():
        raise FileNotFoundError(JERSEY_SHEET)

    crop_badges()
    crop_jerseys()
    crop_status()
    print(f"Imported corrected badges, jerseys, and status icons into {BASE}")


if __name__ == "__main__":
    main()
