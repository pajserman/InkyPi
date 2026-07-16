import fnmatch
import json
import logging
import os
import numpy as np
from PIL import Image

from utils.image_utils import resize_image, change_orientation, apply_image_enhancement
from display.mock_display import MockDisplay

logger = logging.getLogger(__name__)

# Fallback native e-ink palette (7-color Inky Impression) used for color mapping
# when the active display does not expose its own palette (e.g. mock display in
# development). Values are the canonical desaturated RGB targets.
DEFAULT_INKY_PALETTE = [
    [0, 0, 0],        # black
    [255, 255, 255],  # white
    [0, 255, 0],      # green
    [0, 0, 255],      # blue
    [255, 0, 0],      # red
    [255, 255, 0],    # yellow
    [255, 140, 0],    # orange
]

# Color mapping thresholds (all in PIL's 0-255 HSV scale).
# A palette entry is treated as a color (vs. black/white/gray) when its
# max-min channel spread exceeds this.
_CHROMATIC_PALETTE_SPREAD = 20
# Pixels with saturation at or above this are matched by hue (so a light blue
# maps to blue instead of white). Below it, pixels are treated as grayscale.
_PIXEL_SATURATION_THRESHOLD = 40
# Near-black pixels map to the darkest palette color regardless of saturation,
# so deep shadows don't turn into vivid colors.
_PIXEL_DARK_VALUE_THRESHOLD = 32
# Amplitude (per RGB channel, 0-255 scale) of the random noise used for the
# "allow some dithering" mode. Noise is added before snapping to the nearest
# native color, so shades are approximated by a random spatial mix of native
# colors. Random (white) noise avoids the regular grid patterns that ordered or
# error-diffusion dithering produce. Larger values reproduce tone better but
# look grainier; 64 is a good balance — enough to break periodicity without
# producing visible static.
_DITHER_NOISE_AMPLITUDE = 32


# Try to import hardware displays, but don't fail if they're not available
try:
    from display.inky_display import InkyDisplay
except ImportError:
    logger.info("Inky display not available, hardware support disabled")

try:
    from display.waveshare_display import WaveshareDisplay
except ImportError:
    logger.info("Waveshare display not available, hardware support disabled")

class DisplayManager:

    """Manages the display and rendering of images."""

    def __init__(self, device_config):

        """
        Initializes the display manager and selects the correct display type
        based on the configuration.

        Args:
            device_config (object): Configuration object containing display settings.

        Raises:
            ValueError: If an unsupported display type is specified.
        """

        self.device_config = device_config

        # Cache of the last real content image (raw, before processing) plus its
        # render context, used to preview color-mapping setting changes.
        self._last_source_image = None
        self._last_source_settings = []

        display_type = device_config.get_config("display_type", default="inky")

        if display_type == "mock":
            self.display = MockDisplay(device_config)
        elif display_type == "inky":
            self.display = InkyDisplay(device_config)
        elif fnmatch.fnmatch(display_type, "epd*in*"):
            # derived from waveshare epd - we assume here that will be consistent
            # otherwise we will have to enshring the manufacturer in the
            # display_type and then have a display_model parameter.  Will leave
            # that for future use if the need arises.
            #
            # see https://github.com/waveshareteam/e-Paper
            self.display = WaveshareDisplay(device_config)
        else:
            raise ValueError(f"Unsupported display type: {display_type}")

    def display_image(self, image, image_settings=[], plugin_id=None):

        """
        Delegates image rendering to the appropriate display instance.

        Args:
            image (PIL.Image): The image to be displayed.
            image_settings (list, optional): List of settings to modify image rendering.

        Raises:
            ValueError: If no valid display instance is found.
        """

        if not hasattr(self, "display"):
            raise ValueError("No valid display instance initialized.")

        bypass_calibration = (plugin_id or "").lower() == "calibrate"
        # The native_palette test card is intentionally passed through color
        # mapping so its image demonstrates the current mapping settings.
        bypass_color_map = False

        # Remember the last real content image so color-mapping changes can be
        # previewed against it. Skip the calibrate/native_palette helper plugins,
        # whose output is not representative content.
        if (plugin_id or "").lower() not in ("calibrate", "native_palette"):
            try:
                self._last_source_image = image.copy()
                self._last_source_settings = image_settings
                # Persist the raw source to disk so previews survive restarts.
                source_path = getattr(self.device_config, "color_map_source_file", None)
                if source_path:
                    image.convert("RGB").save(source_path)
            except Exception:
                logger.warning("Failed to cache last source image for preview", exc_info=True)

        image = self.prepare_image_for_display(
            image,
            image_settings,
            bypass_calibration=bypass_calibration,
            bypass_color_map=bypass_color_map,
        )

        # Save the processed image so UI preview matches actual rendered output.
        logger.info(f"Saving image to {self.device_config.current_image_file}")
        image.save(self.device_config.current_image_file)

        # Pass to the concrete instance to render to the device.
        self.display.display_image(image, image_settings)

    def get_preview_source(self):
        """Returns the last real content image (raw) and its render settings.

        Tries the in-memory cache first, then the persisted source file (so
        previews work across restarts). Returns (None, []) if unavailable.
        """
        if self._last_source_image is not None:
            return self._last_source_image, self._last_source_settings

        source_path = getattr(self.device_config, "color_map_source_file", None)
        if source_path and os.path.exists(source_path):
            try:
                with Image.open(source_path) as img:
                    return img.copy(), []
            except Exception:
                logger.warning("Failed to load persisted preview source image", exc_info=True)

        return None, []

    def prepare_image_for_display(self, image, image_settings=[], bypass_calibration=False, bypass_color_map=False, color_map_override=None):
        """Apply orientation, resize, calibration, inversion, enhancements and native palette mapping before rendering."""

        # Resize and adjust orientation
        image = change_orientation(image, self.device_config.get_config("orientation"))
        image = resize_image(image, self.device_config.get_resolution(), image_settings)
        if not bypass_calibration:
            image = self._apply_calibration(image)
        if self.device_config.get_config("inverted_image"): image = image.rotate(180)
        image = apply_image_enhancement(image, self.device_config.get_config("image_settings"))
        if not bypass_color_map:
            image = self._apply_color_map(image, color_map_override)
        return image

    def _apply_color_map(self, image, color_map_override=None):
        """Map every pixel onto the native display colors when enabled globally.

        Two modes are supported:
        - Flat (default): a hue-aware nearest-color snap that fills regions with
          solid native colors and no dithering.
        - Allow some dithering: Floyd-Steinberg error diffusion against the native
          palette, so in-between shades are approximated by spatially mixing
          native colors instead of hard-snapping every pixel.
        """
        color_map = color_map_override if color_map_override is not None else self.device_config.get_color_map()
        if not color_map.get("enabled"):
            return image

        palette = self._resolve_native_palette()
        if not palette:
            return image

        if color_map.get("allow_dithering"):
            return self._map_with_dithering(image, palette, color_map)
        return self._map_to_nearest_native(image, palette, color_map)

    def _resolve_native_palette(self):
        """Returns the active display's native color palette, or the default."""
        palette = None
        if hasattr(self.display, "get_palette"):
            palette = self.display.get_palette()
        if not palette:
            palette = DEFAULT_INKY_PALETTE
        return palette

    def _map_with_dithering(self, image, palette, color_map):
        """Map to the native palette using random (white) noise dithering.

        Each pixel is perturbed by random noise and then snapped to the nearest
        native color, so shades the panel cannot show directly are approximated
        by a random spatial mix of native colors. Using random noise (rather than
        ordered or Floyd-Steinberg error diffusion) avoids the regular grid-like
        patterns those methods leave in flat regions.
        """
        pal = np.array([[int(c) for c in color[:3]] for color in palette], dtype=np.float32)
        if pal.shape[0] == 0:
            return image

        amplitude = int(color_map.get("noise_amplitude", _DITHER_NOISE_AMPLITUDE))

        rgb_image = image.convert("RGB")
        rgb = np.asarray(rgb_image, dtype=np.float32)

        if amplitude > 0:
            noise = (np.random.rand(*rgb.shape).astype(np.float32) - 0.5) * amplitude
            rgb = rgb + noise

        # Nearest native color per pixel, computed one palette entry at a time to
        # keep memory usage low on large displays.
        best_dist = None
        best_idx = None
        for i in range(pal.shape[0]):
            diff = rgb - pal[i]
            dist = np.einsum("ijk,ijk->ij", diff, diff)
            if best_dist is None:
                best_dist = dist
                best_idx = np.zeros(dist.shape, dtype=np.intp)
            else:
                closer = dist < best_dist
                best_dist = np.where(closer, dist, best_dist)
                best_idx = np.where(closer, i, best_idx)

        mapped = pal[best_idx].astype(np.uint8)
        return Image.fromarray(mapped, "RGB").convert(image.mode)

    def _map_to_nearest_native(self, image, palette, color_map):
        """Snap every pixel to the nearest native display color, disabling dithering.

        Matching is hue-aware rather than a plain RGB nearest-neighbour: colored
        pixels are matched to the closest palette color by hue (so a light blue
        maps to blue instead of white), while low-saturation pixels are treated
        as grayscale and mapped to black/white by brightness. This produces flat
        solid fills instead of dithered approximations.
        """
        pal = np.array([[int(c) for c in color[:3]] for color in palette], dtype=np.int16)
        if pal.shape[0] == 0:
            return image

        # Split the palette into colored vs. grayscale (black/white) entries.
        pal_spread = pal.max(axis=1) - pal.min(axis=1)
        chromatic_idx = np.where(pal_spread > _CHROMATIC_PALETTE_SPREAD)[0]
        achromatic_idx = np.where(pal_spread <= _CHROMATIC_PALETTE_SPREAD)[0]

        # Palette hue/value in the same 0-255 scale PIL uses for HSV.
        pal_img = Image.fromarray(pal.astype(np.uint8).reshape(1, -1, 3), "RGB")
        pal_hsv = np.asarray(pal_img.convert("HSV"), dtype=np.int16).reshape(-1, 3)
        pal_hue = pal_hsv[:, 0]
        pal_value = pal_hsv[:, 2]

        rgb_image = image.convert("RGB")
        hsv = np.asarray(rgb_image.convert("HSV"), dtype=np.int16).reshape(-1, 3)
        hue, sat, val = hsv[:, 0], hsv[:, 1], hsv[:, 2]

        # A pixel is grayscale if it is barely saturated or nearly black, unless
        # there are no colored palette entries to map onto.
        sat_threshold = int(color_map.get("saturation_threshold", _PIXEL_SATURATION_THRESHOLD))
        is_grayscale = (sat < sat_threshold) | (val < _PIXEL_DARK_VALUE_THRESHOLD)
        if chromatic_idx.size == 0:
            is_grayscale[:] = True
        if achromatic_idx.size == 0:
            is_grayscale[:] = False

        out_idx = np.zeros(hsv.shape[0], dtype=np.intp)

        # Grayscale pixels -> nearest black/white by brightness.
        if achromatic_idx.size:
            gray_pixels = is_grayscale
            if gray_pixels.any():
                value_diff = np.abs(val[gray_pixels][:, None] - pal_value[achromatic_idx][None, :])
                out_idx[gray_pixels] = achromatic_idx[value_diff.argmin(axis=1)]

        # Colored pixels -> nearest palette color by circular hue distance.
        if chromatic_idx.size:
            color_pixels = ~is_grayscale
            if color_pixels.any():
                hue_diff = np.abs(hue[color_pixels][:, None] - pal_hue[chromatic_idx][None, :])
                hue_diff = np.minimum(hue_diff, 256 - hue_diff)
                out_idx[color_pixels] = chromatic_idx[hue_diff.argmin(axis=1)]

        width, height = rgb_image.size
        mapped = pal[out_idx].astype(np.uint8).reshape(height, width, 3)
        return Image.fromarray(mapped, "RGB").convert(image.mode)

    def _apply_calibration(self, image):
        """Fit content into calibrated visible area so frame-clipped edges are avoided globally."""
        calibration = self.device_config.get_calibration()
        if not calibration.get("enabled"):
            return image

        width, height = image.size
        margin_left = max(0, min(int(calibration.get("margin_left", 0)), width - 1))
        margin_right = max(0, min(int(calibration.get("margin_right", 0)), width - 1))
        margin_top = max(0, min(int(calibration.get("margin_top", 0)), height - 1))
        margin_bottom = max(0, min(int(calibration.get("margin_bottom", 0)), height - 1))

        visible_width = width - margin_left - margin_right
        visible_height = height - margin_top - margin_bottom
        if visible_width < 2 or visible_height < 2:
            logger.warning("Calibration margins are too large for image size, skipping calibration.")
            return image

        fitted = image.copy()
        resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
        fitted.thumbnail((visible_width, visible_height), resample)

        if image.mode in ("RGBA", "LA"):
            background = (255, 255, 255, 255)
        elif image.mode == "RGB":
            background = (255, 255, 255)
        elif image.mode == "L":
            background = 255
        elif image.mode == "1":
            background = 1
        else:
            background = (255, 255, 255)

        canvas = Image.new(image.mode, (width, height), background)
        offset_x = margin_left + (visible_width - fitted.width) // 2
        offset_y = margin_top + (visible_height - fitted.height) // 2
        canvas.paste(fitted, (offset_x, offset_y))
        return canvas