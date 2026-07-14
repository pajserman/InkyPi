import fnmatch
import json
import logging
from PIL import Image

from utils.image_utils import resize_image, change_orientation, apply_image_enhancement
from display.mock_display import MockDisplay

logger = logging.getLogger(__name__)

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
        image = self.prepare_image_for_display(image, image_settings, bypass_calibration=bypass_calibration)

        # Save the processed image so UI preview matches actual rendered output.
        logger.info(f"Saving image to {self.device_config.current_image_file}")
        image.save(self.device_config.current_image_file)

        # Pass to the concrete instance to render to the device.
        self.display.display_image(image, image_settings)

    def prepare_image_for_display(self, image, image_settings=[], bypass_calibration=False):
        """Apply orientation, resize, calibration, inversion and enhancements before rendering."""

        # Resize and adjust orientation
        image = change_orientation(image, self.device_config.get_config("orientation"))
        image = resize_image(image, self.device_config.get_resolution(), image_settings)
        if not bypass_calibration:
            image = self._apply_calibration(image)
        if self.device_config.get_config("inverted_image"): image = image.rotate(180)
        image = apply_image_enhancement(image, self.device_config.get_config("image_settings"))
        return image

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