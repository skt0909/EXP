from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


BASE = Path(__file__).resolve().parents[1] / "public" / "assets" / "premier-league" / "2026-27"

TEAMS = [
    ("arsenal", "ARS", "#db0007", "#ffffff", "#063672"),
    ("aston-villa", "AVL", "#95bfe5", "#7a003c", "#f3d459"),
    ("bournemouth", "BOU", "#d71920", "#111111", "#ffffff"),
    ("brentford", "BRE", "#e30613", "#ffffff", "#111111"),
    ("brighton", "BHA", "#0057b8", "#ffffff", "#ffcd00"),
    ("chelsea", "CHE", "#034694", "#ffffff", "#dba111"),
    ("coventry-city", "COV", "#77c7e8", "#ffffff", "#1b365d"),
    ("crystal-palace", "CRY", "#1b458f", "#c4122e", "#ffffff"),
    ("everton", "EVE", "#003399", "#ffffff", "#ffffff"),
    ("fulham", "FUL", "#ffffff", "#111111", "#cc0000"),
    ("hull-city", "HUL", "#f5a800", "#111111", "#ffffff"),
    ("ipswich-town", "IPS", "#0057b8", "#ffffff", "#e21a23"),
    ("leeds-united", "LEE", "#ffffff", "#1d428a", "#ffcd00"),
    ("liverpool", "LIV", "#c8102e", "#ffffff", "#00b2a9"),
    ("manchester-city", "MCI", "#6cabdd", "#ffffff", "#1c2c5b"),
    ("manchester-united", "MUN", "#da291c", "#ffffff", "#fbe122"),
    ("newcastle-united", "NEW", "#111111", "#ffffff", "#41b6e6"),
    ("nottingham-forest", "NFO", "#dd0000", "#ffffff", "#ffffff"),
    ("sunderland", "SUN", "#eb172b", "#ffffff", "#111111"),
    ("tottenham-hotspur", "TOT", "#ffffff", "#132257", "#132257"),
]

STRIPED = {
    "bournemouth",
    "brentford",
    "brighton",
    "crystal-palace",
    "newcastle-united",
    "sunderland",
}


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


def make_team_icon(slug, abbr, primary, secondary, accent):
    p = rgb(primary)
    s = rgb(secondary)
    a = rgb(accent)
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.ellipse((18, 18, 238, 238), fill=darken(p, 0.88), outline=(255, 255, 255, 255), width=8)
    draw.ellipse((34, 34, 222, 222), fill=p, outline=a, width=6)
    shield = [(128, 50), (200, 96), (176, 198), (128, 224), (80, 198), (56, 96)]
    draw.polygon(shield, fill=lighten(p, 0.12), outline=s, width=5)

    if slug in STRIPED:
        for x in range(62, 198, 48):
            draw.rectangle((x, 62, x + 24, 204), fill=s)
        draw.polygon(shield, outline=a, width=4)

    text_fill = s if slug not in {"fulham", "leeds-united", "tottenham-hotspur"} else p
    centered(draw, (45, 74, 211, 172), abbr, load_font(58), text_fill)
    centered(draw, (54, 172, 202, 204), "2026", load_font(22, False), a if a != (255, 255, 255) else s)
    return image


def make_jersey(slug, primary, secondary):
    p = rgb(primary)
    s = rgb(secondary)
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle((68, 57, 188, 226), radius=18, fill=(0, 0, 0, 45))
    shirt = [(80, 54), (106, 38), (150, 38), (176, 54), (224, 82), (198, 128), (182, 114), (182, 226), (74, 226), (74, 114), (58, 128), (32, 82)]
    draw.polygon(shirt, fill=p)
    draw.line(shirt + [shirt[0]], fill=darken(p, 0.72), width=4, joint="curve")
    draw.polygon([(106, 38), (128, 67), (150, 38)], fill=s)
    draw.arc((107, 29, 149, 75), 0, 180, fill=darken(s, 0.82), width=4)

    if slug in STRIPED:
        for x in range(75, 184, 24):
            draw.rectangle((x, 61, x + 12, 224), fill=s)
        draw.polygon(shirt, outline=darken(p, 0.7))
    elif slug == "aston-villa":
        draw.polygon([(32, 82), (80, 54), (74, 114), (58, 128)], fill=s)
        draw.polygon([(176, 54), (224, 82), (198, 128), (182, 114)], fill=s)
    elif slug == "hull-city":
        for x in range(76, 183, 18):
            draw.rectangle((x, 61, x + 9, 224), fill=s)
    else:
        draw.line((78, 92, 182, 92), fill=lighten(p, 0.25), width=5)
        draw.line((84, 130, 176, 130), fill=lighten(p, 0.18), width=4)

    draw.line((74, 224, 182, 224), fill=darken(p, 0.65), width=5)
    draw.line((38, 84, 61, 123), fill=darken(p, 0.65), width=5)
    draw.line((218, 84, 195, 123), fill=darken(p, 0.65), width=5)
    return image


def main():
    team_dir = BASE / "teams"
    jersey_dir = BASE / "jerseys"
    status_dir = BASE / "status"
    for directory in (team_dir, jersey_dir, status_dir):
        directory.mkdir(parents=True, exist_ok=True)

    for slug, abbr, primary, secondary, accent in TEAMS:
        make_team_icon(slug, abbr, primary, secondary, accent).save(team_dir / f"{slug}.png")
        make_jersey(slug, primary, secondary).save(jersey_dir / f"{slug}.png")

    (status_dir / ".gitkeep").write_text("", encoding="utf-8")
    print(f"Generated {len(TEAMS)} team icons and {len(TEAMS)} blank jerseys in {BASE}")


if __name__ == "__main__":
    main()
