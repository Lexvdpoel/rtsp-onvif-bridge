"""Draw the Unraid container icon: a bullet security camera.

Rendered at 4x and downsampled, which is the cheap way to get clean edges
without an anti-aliasing draw backend. Unraid shows this at roughly 48px, so
the glyph is kept to a few bold shapes that survive that size.
"""
import math
import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
SIZE = 256
SCALE = 4
S = SIZE * SCALE

BG = (42, 120, 214, 255)        # palette slot 1, reads on Unraid's dark UI
BODY = (255, 255, 255, 255)
LENS_RIM = (42, 120, 214, 255)
LENS = (24, 38, 56, 255)
GLINT = (255, 255, 255, 235)

image = Image.new("RGBA", (S, S), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)

# Rounded-square plate.
draw.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=BG)


def rotated_rect(cx, cy, w, h, radius, angle_deg, fill):
    """Draw a rounded rectangle rotated about its centre."""
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], radius=radius, fill=fill
    )
    image.alpha_composite(layer.rotate(angle_deg, center=(cx, cy), resample=Image.BICUBIC))


cx, cy = S * 0.50, S * 0.44
angle = 12  # a slight downward tilt reads as "aimed at something"

# Wall mount: arm down to a base plate, drawn first so the body covers its top.
rotated_rect(S * 0.62, S * 0.70, S * 0.075, S * 0.30, S * 0.035, 0, BODY)
draw.rounded_rectangle(
    [S * 0.44, S * 0.815, S * 0.80, S * 0.875], radius=S * 0.03, fill=BODY
)

# Camera body and the sun hood over it.
rotated_rect(cx, cy, S * 0.62, S * 0.30, S * 0.14, angle, BODY)
rotated_rect(cx - S * 0.015, cy - S * 0.115, S * 0.50, S * 0.075, S * 0.035, angle, BODY)

# Lens at the front (left) end of the tilted body.
rad = math.radians(angle)
lx = cx - math.cos(rad) * S * 0.255
ly = cy + math.sin(rad) * S * 0.255
for radius, fill in ((S * 0.125, BODY), (S * 0.105, LENS_RIM), (S * 0.072, LENS)):
    draw.ellipse([lx - radius, ly - radius, lx + radius, ly + radius], fill=fill)

# A single highlight so the lens reads as glass, not a hole.
gx, gy, gr = lx - S * 0.028, ly - S * 0.030, S * 0.022
draw.ellipse([gx - gr, gy - gr, gx + gr, gy + gr], fill=GLINT)

image.resize((SIZE, SIZE), Image.LANCZOS).save(OUT)
print("wrote", OUT)

# Also render it at the size Unraid actually displays, to check it holds up.
preview = Image.new("RGBA", (SIZE, 64), (32, 34, 37, 255))
small = image.resize((48, 48), Image.LANCZOS)
for i, x in enumerate((8, 72, 136)):
    preview.alpha_composite(small.resize((48 - i * 12, 48 - i * 12), Image.LANCZOS),
                            (x, 8 + i * 6))
preview.save(os.path.join(os.path.dirname(OUT), "icon_preview.png"))
print("wrote preview")
