import numpy as np
from PIL import Image, ImageDraw, ImageFont

from plugins.base_plugin.base_plugin import BasePlugin


class NativePalette(BasePlugin):
    """Global toggle plugin that maps all displayed colors to the nearest native
    e-ink color (no dithering).

    When enabled, the display pipeline snaps every pixel of every image to the
    closest color the panel can show, so solid areas fill flat with native colors
    instead of dithered approximations.
    """

    @staticmethod
    def _to_bool(settings, key, default=True):
        value = settings.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return default

    def generate_image(self, settings, device_config):
        enabled = self._to_bool(settings, "apply_global", True)
        allow_dithering = self._to_bool(settings, "allow_dithering", False)

        color_map = {
            "enabled": enabled,
            "allow_dithering": allow_dithering,
            "noise_amplitude": settings.get("noise_amplitude", 64),
            "saturation_threshold": settings.get("saturation_threshold", 40),
        }
        device_config.set_color_map(color_map, write=True)

        width, height = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            width, height = height, width

        return self._draw_test_card(width, height, enabled, allow_dithering)

    def _draw_test_card(self, width, height, enabled, allow_dithering):
        """Render a color test card (spectrums, gradients and off-palette swatches).

        The card contains a range of colors the panel cannot show directly so
        that, once color mapping is applied, you can judge how well the mapping
        reproduces them.
        """
        title_h = int(height * 0.12)
        caption_h = int(height * 0.10)
        content_top = title_h
        content_h = height - title_h - caption_h
        band_h = content_h // 4

        # Columns 0..255 across the width, used to sweep hue/value.
        xs = (np.arange(width) / max(width - 1, 1) * 255).astype(np.uint8)

        # Default everything to white (S=0, V=255) in HSV.
        hsv = np.zeros((height, width, 3), dtype=np.uint8)
        hsv[..., 2] = 255

        # Band 1: full-saturation hue spectrum.
        a0, a1 = content_top, content_top + band_h
        hsv[a0:a1, :, 0] = xs[None, :]
        hsv[a0:a1, :, 1] = 255
        hsv[a0:a1, :, 2] = 255

        # Band 2: pastel (low-saturation) hue spectrum - tests light colors.
        b0, b1 = a1, a1 + band_h
        hsv[b0:b1, :, 0] = xs[None, :]
        hsv[b0:b1, :, 1] = 70
        hsv[b0:b1, :, 2] = 255

        # Band 3: grayscale ramp (black -> white).
        c0, c1 = b1, b1 + band_h
        hsv[c0:c1, :, 0] = 0
        hsv[c0:c1, :, 1] = 0
        hsv[c0:c1, :, 2] = xs[None, :]

        rgb = np.asarray(Image.fromarray(hsv, "HSV").convert("RGB"))
        image = Image.fromarray(rgb.copy(), "RGB")
        draw = ImageDraw.Draw(image)

        # Band 4: solid off-palette swatches (colors between native palette entries).
        swatches = [
            (255, 120, 80), (120, 200, 80), (80, 160, 220), (180, 90, 200),
            (230, 200, 90), (90, 190, 190), (240, 150, 190), (150, 110, 70),
        ]
        d0, d1 = c1, content_top + content_h
        count = len(swatches)
        swatch_w = width / count
        for i, color in enumerate(swatches):
            x0 = int(i * swatch_w)
            x1 = int((i + 1) * swatch_w)
            draw.rectangle((x0, d0, x1, d1), fill=color)

        title_font = self._load_font(bold=True, size=max(int(height * 0.06), 14))
        caption_font = self._load_font(bold=False, size=max(int(height * 0.035), 10))

        mode = "OFF"
        if enabled:
            mode = "ON (dithered)" if allow_dithering else "ON (flat)"
        draw.text((width // 2, title_h // 2), f"Native Palette Test - Mapping: {mode}",
                  fill="black", anchor="mm", font=title_font)

        caption = ("Rows: saturated hues, pastels, grayscale, off-palette swatches. "
                   "Compare against the native colors.")
        draw.text((width // 2, height - caption_h // 2), caption,
                  fill="black", anchor="mm", font=caption_font)

        return image

    @staticmethod
    def _load_font(bold, size):
        candidates = (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        )
        try:
            return ImageFont.truetype(candidates, size)
        except (OSError, IOError):
            return ImageFont.load_default()
