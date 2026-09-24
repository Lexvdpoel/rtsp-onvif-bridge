"""Draw the Unraid container icons: a bullet security camera on a coloured plate.

Two variants are produced. Blue is the controller, red is a virtual camera, so
the Docker tab separates the one management container from the many cameras at a
glance.

Rendered at 4x and downsampled, which is the cheap way to get clean edges
without an anti-aliasing draw backend. Unraid shows these at roughly 48px, so
the glyph is kept to a few bold shapes that survive that size.

    python unraid/make_icon.py
"""
import math
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SIZE = 256
SCALE = 4
S = SIZE * SCALE

# Both plates clear 4.5:1 against the white glyph, so the camera stays legible.
CONTROLLER_BG = (42, 120, 214, 255)   # palette slot 1, blue
CAMERA_BG = (211, 58, 52, 255)        # palette slot 8, red, deepened for contrast

BODY = (255, 255, 255, 255)
LENS = (24, 38, 56, 255)
GLINT = (255, 255, 255, 235)


def render(background):
    image = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=background)

    def rotated_rect(cx, cy, w, h, radius, angle_deg, fill):
        """Draw a rounded rectangle rotated about its centre."""
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle(
            [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], radius=radius, fill=fill
        )
        image.alpha_composite(
            layer.rotate(angle_deg, center=(cx, cy), resample=Image.BICUBIC)
        )

    cx, cy = S * 0.50, S * 0.44
    angle = 12  # a slight downward tilt reads as "aimed at something"

    # Wall mount: arm down to a base plate, drawn first so the body covers its top.
    rotated_rect(S * 0.62, S * 0.70, S * 0.075, S * 0.30, S * 0.035, 0, BODY)
    draw.rounded_rectangle(
        [S * 0.44, S * 0.815, S * 0.80, S * 0.875], radius=S * 0.03, fill=BODY
    )

    # Camera body and the sun hood over it.
    rotated_rect(cx, cy, S * 0.62, S * 0.30, S * 0.14, angle, BODY)
    rotated_rect(cx - S * 0.015, cy - S * 0.115, S * 0.50, S * 0.075, S * 0.035,
                 angle, BODY)

    # Lens at the front (left) end of the tilted body. The rim picks up the plate
    # colour so the lens reads as part of the same object.
    rad = math.radians(angle)
    lx = cx - math.cos(rad) * S * 0.255
    ly = cy + math.sin(rad) * S * 0.255
    for radius, fill in ((S * 0.125, BODY), (S * 0.105, background), (S * 0.072, LENS)):
        draw.ellipse([lx - radius, ly - radius, lx + radius, ly + radius], fill=fill)

    # A single highlight so the lens reads as glass, not a hole.
    gx, gy, gr = lx - S * 0.028, ly - S * 0.030, S * 0.022
    draw.ellipse([gx - gr, gy - gr, gx + gr, gy + gr], fill=GLINT)

    return image.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    for name, background in (("icon.png", CONTROLLER_BG),
                             ("icon-camera.png", CAMERA_BG)):
        path = os.path.join(HERE, name)
        render(background).save(path)
        print("wrote", path)


if __name__ == "__main__":
    main()
