from flask import Blueprint, request, jsonify, current_app, render_template, send_from_directory, send_file
from plugins.plugin_registry import get_plugin_instance
from utils.app_utils import resolve_path, handle_request_files, parse_form
from refresh_task import ManualRefresh, PlaylistRefresh
import json
import os
import logging
from io import BytesIO
from PIL import Image

logger = logging.getLogger(__name__)
plugin_bp = Blueprint("plugin", __name__)

def _delete_plugin_instance_images(device_config, plugin_instance_obj):
    """Delete all images associated with a plugin instance."""
    # Delete the plugin instance's generated image
    plugin_image_path = os.path.join(device_config.plugin_image_dir, plugin_instance_obj.get_image_path())
    if os.path.exists(plugin_image_path):
        try:
            os.remove(plugin_image_path)
            logger.info(f"Deleted plugin instance image: {plugin_image_path}")
        except Exception as e:
            logger.warning(f"Failed to delete plugin instance image {plugin_image_path}: {e}")

    # Call the plugin's cleanup method to handle plugin-specific resource cleanup
    try:
        plugin_config = device_config.get_plugin(plugin_instance_obj.plugin_id)
        if plugin_config:
            plugin = get_plugin_instance(plugin_config)
            plugin.cleanup(plugin_instance_obj.settings)
    except Exception as e:
        logger.warning(f"Error during plugin cleanup for {plugin_instance_obj.plugin_id}: {e}")

# Removed module-level PLUGINS_DIR - will resolve dynamically in route handlers

@plugin_bp.route('/plugin/<plugin_id>')
def plugin_page(plugin_id):
    device_config = current_app.config['DEVICE_CONFIG']
    playlist_manager = device_config.get_playlist_manager()

    # Find the plugin by id
    plugin_config = device_config.get_plugin(plugin_id)
    if plugin_config:
        try:
            plugin = get_plugin_instance(plugin_config)
            template_params = plugin.generate_settings_template()

            if plugin_id == "calibrate":
                calibration = device_config.get_calibration()
                calibration_tool = device_config.get_calibration_tool()
                template_params["calibration_defaults"] = {
                    "apply_global": "true" if calibration.get("enabled") else "false",
                    "margin_top": calibration.get("margin_top", 0),
                    "margin_right": calibration.get("margin_right", 0),
                    "margin_bottom": calibration.get("margin_bottom", 0),
                    "margin_left": calibration.get("margin_left", 0),
                    "grid_step": calibration_tool.get("grid_step", 50),
                }

            if plugin_id == "native_palette":
                color_map = device_config.get_color_map()
                template_params["color_map_defaults"] = {
                    "apply_global": "true" if color_map.get("enabled") else "false",
                    "allow_dithering": "true" if color_map.get("allow_dithering") else "false",
                    "noise_amplitude": color_map.get("noise_amplitude", 64),
                    "saturation_threshold": color_map.get("saturation_threshold", 40),
                }

            # retrieve plugin instance from the query parameters if updating existing plugin instance
            plugin_instance_name = request.args.get('instance')
            if plugin_instance_name:
                plugin_instance = playlist_manager.find_plugin(plugin_id, plugin_instance_name)
                if not plugin_instance:
                    return jsonify({"error": f"Plugin instance: {plugin_instance_name} does not exist"}), 500

                # add plugin instance settings to the template to prepopulate
                template_params["plugin_settings"] = plugin_instance.settings
                template_params["plugin_instance"] = plugin_instance_name
                template_params["refresh_settings"] = plugin_instance.refresh

            template_params["playlists"] = playlist_manager.get_playlist_names()
        except Exception as e:
            logger.exception("EXCEPTION CAUGHT: " + str(e))
            return jsonify({"error": f"An error occurred: {str(e)}"}), 500
        return render_template('plugin.html', plugin=plugin_config, **template_params)
    else:
        return "Plugin not found", 404

@plugin_bp.route('/native_palette/apply', methods=['POST'])
def native_palette_apply():
    """Persist native palette color mapping settings from the plugin page."""
    device_config = current_app.config['DEVICE_CONFIG']
    try:
        form = request.form
        color_map = {
            "enabled": form.get("apply_global"),
            "allow_dithering": form.get("allow_dithering"),
            "noise_amplitude": form.get("noise_amplitude"),
            "saturation_threshold": form.get("saturation_threshold"),
        }
        device_config.set_color_map(color_map, write=True)
    except Exception as e:
        logger.exception("EXCEPTION CAUGHT: " + str(e))
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500
    return jsonify({"success": True, "message": "Applied color mapping settings."})

@plugin_bp.route('/native_palette/preview')
def native_palette_preview():
    """Render a preview with the current color mapping settings.

    Query param `source` selects what is mapped:
    - "last" (default): the last displayed content image.
    - "palette": the plugin's color test card.
    """
    device_config = current_app.config['DEVICE_CONFIG']
    display_manager = current_app.config['DISPLAY_MANAGER']

    # Optional live-preview overrides from query params (used while dragging the
    # sliders, before the settings are saved).
    color_map_override = None
    override_keys = ("apply_global", "allow_dithering", "noise_amplitude", "saturation_threshold")
    if any(key in request.args for key in override_keys):
        def _truthy(value):
            return str(value).lower() in ("true", "1", "on", "yes")
        color_map_override = {
            "enabled": _truthy(request.args.get("apply_global", "false")),
            "allow_dithering": _truthy(request.args.get("allow_dithering", "false")),
            "noise_amplitude": request.args.get("noise_amplitude", 64),
            "saturation_threshold": request.args.get("saturation_threshold", 40),
        }

    source_kind = request.args.get("source", "last")
    image_settings = []

    if source_kind == "palette":
        # Build the plugin's color test card at the display resolution.
        plugin_config = device_config.get_plugin("native_palette")
        plugin = get_plugin_instance(plugin_config)
        width, height = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            width, height = height, width
        effective = color_map_override if color_map_override is not None else device_config.get_color_map()
        source = plugin._draw_test_card(
            width, height, bool(effective.get("enabled")), bool(effective.get("allow_dithering")))
    else:
        source, image_settings = display_manager.get_preview_source()
        if source is None:
            # Fall back to the last displayed image if no source has been cached yet.
            image_path = device_config.current_image_file
            if not os.path.exists(image_path):
                return "No image available", 404
            with Image.open(image_path) as img:
                source = img.copy()

    try:
        processed = display_manager.prepare_image_for_display(
            source.copy(), image_settings=image_settings, color_map_override=color_map_override)
    except Exception as e:
        logger.exception("EXCEPTION CAUGHT: " + str(e))
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500

    img_io = BytesIO()
    processed.convert("RGB").save(img_io, "PNG")
    img_io.seek(0)

    response = send_file(img_io, mimetype='image/png')
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response

@plugin_bp.route('/images/<plugin_id>/<path:filename>')
def image(plugin_id, filename):
    # Resolve plugins directory dynamically
    plugins_dir = resolve_path("plugins")

    # Construct the full path to the plugin's file
    plugin_dir = os.path.join(plugins_dir, plugin_id)

    # Security check to prevent directory traversal
    safe_path = os.path.abspath(os.path.join(plugin_dir, filename))
    if not safe_path.startswith(os.path.abspath(plugins_dir)):
        return "Invalid path", 403

    # Convert to absolute path for send_from_directory
    abs_plugin_dir = os.path.abspath(plugin_dir)

    # Check if the directory and file exist
    if not os.path.isdir(abs_plugin_dir):
        logger.error(f"Plugin directory not found: {abs_plugin_dir}")
        return "Plugin directory not found", 404

    if not os.path.isfile(safe_path):
        logger.error(f"File not found: {safe_path}")
        return "File not found", 404

    # Serve the file from the plugin directory
    return send_from_directory(abs_plugin_dir, filename)

@plugin_bp.route('/plugin_instance_image/<path:playlist_name>/<path:plugin_id>/<path:instance_name>')
def plugin_instance_image(playlist_name, plugin_id, instance_name):
    """Serve the generated image for a plugin instance."""
    device_config = current_app.config['DEVICE_CONFIG']
    playlist_manager = device_config.get_playlist_manager()

    # Find the plugin instance
    playlist = playlist_manager.get_playlist(playlist_name)
    if not playlist:
        return "Playlist not found", 404

    plugin_instance = playlist.find_plugin(plugin_id, instance_name)
    if not plugin_instance:
        return "Plugin instance not found", 404

    # Get the image path
    image_filename = plugin_instance.get_image_path()
    image_path = os.path.join(device_config.plugin_image_dir, image_filename)

    # Check if the image exists
    if not os.path.exists(image_path):
        # Return a placeholder or 404
        return "Image not yet generated", 404

    plugin_config = device_config.get_plugin(plugin_id)
    image_settings = plugin_config.get("image_settings", []) if plugin_config else []
    display_manager = current_app.config['DISPLAY_MANAGER']

    with Image.open(image_path) as img:
        processed = display_manager.prepare_image_for_display(
            img.copy(),
            image_settings=image_settings,
            bypass_calibration=(plugin_id.lower() == "calibrate")
        )

    img_io = BytesIO()
    processed.save(img_io, "PNG")
    img_io.seek(0)

    response = send_file(img_io, mimetype='image/png')
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response

@plugin_bp.route('/delete_plugin_instance', methods=['POST'])
def delete_plugin_instance():
    device_config = current_app.config['DEVICE_CONFIG']
    playlist_manager = device_config.get_playlist_manager()

    data = request.json
    playlist_name = data.get("playlist_name")
    plugin_id = data.get("plugin_id")
    plugin_instance = data.get("plugin_instance")

    try:
        playlist = playlist_manager.get_playlist(playlist_name)
        if not playlist:
            return jsonify({"success": False, "message": "Playlist not found"}), 400

        # Get the plugin instance to find associated images
        plugin_instance_obj = playlist.find_plugin(plugin_id, plugin_instance)
        if not plugin_instance_obj:
            return jsonify({"success": False, "message": "Plugin instance not found"}), 400

        # Delete associated images before removing from playlist
        _delete_plugin_instance_images(device_config, plugin_instance_obj)

        result = playlist.delete_plugin(plugin_id, plugin_instance)
        if not result:
            return jsonify({"success": False, "message": "Plugin instance not found"}), 400

        # save changes to device config file
        device_config.write_config()

    except Exception as e:
        logger.exception("EXCEPTION CAUGHT: " + str(e))
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500

    return jsonify({"success": True, "message": "Deleted plugin instance."})

@plugin_bp.route('/update_plugin_instance/<string:instance_name>', methods=['PUT'])
def update_plugin_instance(instance_name):
    device_config = current_app.config['DEVICE_CONFIG']
    playlist_manager = device_config.get_playlist_manager()

    try:
        form_data = parse_form(request.form)

        if not instance_name:
            raise RuntimeError("Instance name is required")

        plugin_id = form_data.pop("plugin_id")
        plugin_instance = playlist_manager.find_plugin(plugin_id, instance_name)
        if not plugin_instance:
            return jsonify({"error": f"Plugin instance: {instance_name} does not exist"}), 500

        # Handle refresh settings if provided
        refresh_settings_json = form_data.pop("refresh_settings", None)
        if refresh_settings_json:
            from utils.time_utils import calculate_seconds
            refresh_settings = json.loads(refresh_settings_json)
            refresh_type = refresh_settings.get('refreshType')

            if refresh_type == "interval":
                unit = refresh_settings.get('unit')
                interval = refresh_settings.get('interval')
                if unit and interval:
                    refresh_interval_seconds = calculate_seconds(int(interval), unit)
                    plugin_instance.refresh = {"interval": refresh_interval_seconds}
            elif refresh_type == "scheduled":
                refresh_time = refresh_settings.get('refreshTime')
                if refresh_time:
                    plugin_instance.refresh = {"scheduled": refresh_time}

        # Only update plugin settings if there's actual data (not just refresh settings)
        plugin_settings = form_data
        plugin_settings.update(handle_request_files(request.files, request.form))

        if plugin_settings:  # Only update if there are actual plugin settings
            plugin_instance.settings = plugin_settings

        device_config.write_config()
    except Exception as e:
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500
    return jsonify({"success": True, "message": f"Updated plugin instance {instance_name}."})

@plugin_bp.route('/display_plugin_instance', methods=['POST'])
def display_plugin_instance():
    device_config = current_app.config['DEVICE_CONFIG']
    refresh_task = current_app.config['REFRESH_TASK']
    playlist_manager = device_config.get_playlist_manager()

    data = request.json
    playlist_name = data.get("playlist_name")
    plugin_id = data.get("plugin_id")
    plugin_instance_name = data.get("plugin_instance")

    try:
        playlist = playlist_manager.get_playlist(playlist_name)
        if not playlist:
            return jsonify({"success": False, "message": f"Playlist {playlist_name} not found"}), 400

        plugin_instance = playlist.find_plugin(plugin_id, plugin_instance_name)
        if not plugin_instance:
            return jsonify({"success": False, "message": f"Plugin instance '{plugin_instance_name}' not found"}), 400

        refresh_task.manual_update(PlaylistRefresh(playlist, plugin_instance, force=True))
    except Exception as e:
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500

    return jsonify({"success": True, "message": "Display updated"}), 200

@plugin_bp.route('/update_now', methods=['POST'])
def update_now():
    device_config = current_app.config['DEVICE_CONFIG']
    refresh_task = current_app.config['REFRESH_TASK']
    display_manager = current_app.config['DISPLAY_MANAGER']

    try:
        plugin_settings = parse_form(request.form)
        plugin_settings.update(handle_request_files(request.files))
        plugin_id = plugin_settings.pop("plugin_id")

        # Check if refresh task is running
        if refresh_task.running:
            refresh_task.manual_update(ManualRefresh(plugin_id, plugin_settings))
        else:
            # In development mode, directly update the display
            logger.info("Refresh task not running, updating display directly")
            plugin_config = device_config.get_plugin(plugin_id)
            if not plugin_config:
                return jsonify({"error": f"Plugin '{plugin_id}' not found"}), 404

            plugin = get_plugin_instance(plugin_config)
            image = plugin.generate_image(plugin_settings, device_config)
            display_manager.display_image(
                image,
                image_settings=plugin_config.get("image_settings", []),
                plugin_id=plugin_id
            )

    except Exception as e:
        logger.exception(f"Error in update_now: {str(e)}")
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500

    return jsonify({"success": True, "message": "Display updated"}), 200
