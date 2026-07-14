from PIL import Image, ImageDraw, ImageFont

from plugins.base_plugin.base_plugin import BasePlugin


class Calibrate(BasePlugin):
    @staticmethod
    def _to_int(settings, key, default, min_value=0, max_value=10000):
        try:
            value = int(settings.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(min_value, min(value, max_value))

    @staticmethod
    def _to_bool(settings, key, default=False):
        value = settings.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes", "on")
        return default

    def generate_image(self, settings, device_config):
        width, height = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            width, height = height, width

        margin_left = self._to_int(settings, "margin_left", 0, 0, width // 2)
        margin_right = self._to_int(settings, "margin_right", 0, 0, width // 2)
        margin_top = self._to_int(settings, "margin_top", 0, 0, height // 2)
        margin_bottom = self._to_int(settings, "margin_bottom", 0, 0, height // 2)
        grid_step = self._to_int(settings, "grid_step", 50, 20, 400)
        apply_global = self._to_bool(settings, "apply_global", True)

        calibration = {
            "enabled": apply_global,
            "margin_left": margin_left,
            "margin_right": margin_right,
            "margin_top": margin_top,
            "margin_bottom": margin_bottom,
        }
        if device_config.get_calibration() != calibration:
            device_config.set_calibration(calibration, write=True)

        calibration_tool = {"grid_step": grid_step}
        if device_config.get_calibration_tool() != calibration_tool:
            device_config.set_calibration_tool(calibration_tool, write=True)

        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default()

        # Outer border shows true panel boundary.
        draw.rectangle((0, 0, width - 1, height - 1), outline="black", width=3)

        # Grid and ruler ticks make clipping easy to measure.
        for x in range(0, width, grid_step):
            draw.line((x, 0, x, height - 1), fill="black", width=1)
            tick = 20 if x % (grid_step * 2) == 0 else 10
            draw.line((x, 0, x, tick), fill="black", width=2)
            if x > 0 and x % (grid_step * 2) == 0:
                draw.text((x + 2, 2), str(x), fill="black", font=font)

        for y in range(0, height, grid_step):
            draw.line((0, y, width - 1, y), fill="black", width=1)
            tick = 20 if y % (grid_step * 2) == 0 else 10
            draw.line((0, y, tick, y), fill="black", width=2)
            if y > 0 and y % (grid_step * 2) == 0:
                draw.text((2, y + 2), str(y), fill="black", font=font)

        # Center crosshair helps check alignment.
        center_x = width // 2
        center_y = height // 2
        draw.line((center_x - 40, center_y, center_x + 40, center_y), fill="black", width=3)
        draw.line((center_x, center_y - 40, center_x, center_y + 40), fill="black", width=3)

        # Safe area rectangle represents user-measured visible area.
        safe_left = margin_left
        safe_top = margin_top
        safe_right = width - 1 - margin_right
        safe_bottom = height - 1 - margin_bottom

        if safe_right > safe_left and safe_bottom > safe_top:
            draw.rectangle((safe_left, safe_top, safe_right, safe_bottom), outline="black", width=5)
            label = f"VISIBLE AREA {safe_right - safe_left + 1}x{safe_bottom - safe_top + 1}"
            draw.text((safe_left + 8, safe_top + 8), label, fill="black", font=font)

        draw.text((8, height - 52), "Adjust margins until the visible box fits your frame window.", fill="black", font=font)
        draw.text((8, height - 36), f"Global calibration: {'ON' if apply_global else 'OFF'}", fill="black", font=font)
        draw.text((8, height - 20), f"L:{margin_left}px  R:{margin_right}px  T:{margin_top}px  B:{margin_bottom}px", fill="black", font=font)

        corners = [
            (8, 8, "0,0"),
            (width - 54, 8, f"{width - 1},0"),
            (8, height - 16, f"0,{height - 1}"),
            (width - 90, height - 16, f"{width - 1},{height - 1}"),
        ]

        for x, y, label in corners:
            draw.text((x, y), label, fill="black", font=font)

        return image
