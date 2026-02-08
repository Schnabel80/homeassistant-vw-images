# VW Images – Home Assistant Integration

A custom Home Assistant integration that provides vehicle images from your Volkswagen WeConnect account.

## Features

- Provides **4 different image types** per vehicle (see [Entities](#entities))
- On-demand updates only (no cyclic polling) via button press or service call
- Rate limiting to prevent API overuse (60 seconds minimum between requests)
- Re-authentication flow for password changes
- German and English UI support

## Installation

### Manual Installation

1. Copy the `custom_components/vw_images/` folder into your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant
3. Go to **Settings > Devices & Services > Add Integration**
4. Search for **VW Images**
5. Enter your Volkswagen WeConnect credentials

### HACS (Custom Repository)

1. Open HACS in your Home Assistant
2. Click the three dots menu (top right) > **Custom repositories**
3. Enter: `https://github.com/Schnabel80/homeassistant-vw-images`
4. Select category: **Integration**
5. Click **Add**
6. Search for **VW Images** in HACS and install it
7. Restart Home Assistant

## Entities

For each vehicle in your account, the integration creates the following entities:

### Image Entities

| Entity | Picture Key | View | Dynamic | Description |
|---|---|---|---|---|
| **Vehicle Image** | `car` | 3/4 side view | No | Static photo of your vehicle. Always the same regardless of vehicle state. |
| **Vehicle Image with Badges** | `carWithBadge` | 3/4 side view | Yes | Same side view with overlay badges showing charging, lock/unlock, heating/cooling, and warning states. |
| **Vehicle Status** | `status` | Bird's eye view | Yes | Top-down view with overlays for open/closed doors, windows, and lights. Changes when you open a door or window. |
| **Vehicle Status with Badges** | `statusWithBadge` | Bird's eye view | Yes | Same top-down view with additional badges for charging, lock/unlock, heating/cooling, parking, and warning states. Most similar to the VW app. |

### Button Entity

| Entity | Description |
|---|---|
| **Update Image** | Press to refresh all images from VW WeConnect. |

> **Note:** Only image types available for your vehicle are created. The number of entities may vary depending on your vehicle model and WeConnect capabilities.

## Usage

### Button

Each vehicle gets an **Update Image** button entity. Press it to refresh all vehicle images from VW WeConnect.

### Service Call

You can also trigger an update via the `vw_images.update_images` service:

```yaml
# Update all vehicles
service: vw_images.update_images

# Update a specific vehicle
service: vw_images.update_images
data:
  vin: "WVWZZZ3CZ9E123456"
```

### Automation Examples

You can automatically refresh vehicle images based on sensor changes. Here are some practical examples:

**Refresh images when a door opens or closes:**

```yaml
automation:
  - alias: "VW Images: Update on door change"
    trigger:
      - platform: state
        entity_id: binary_sensor.your_vw_door_lock
    action:
      - delay: "00:00:05"
      - service: vw_images.update_images
```

**Refresh images when the charging state changes:**

```yaml
automation:
  - alias: "VW Images: Update on charging change"
    trigger:
      - platform: state
        entity_id: sensor.your_vw_charging_state
    action:
      - delay: "00:00:10"
      - service: vw_images.update_images
```

**Refresh images once every morning:**

```yaml
automation:
  - alias: "VW Images: Daily morning update"
    trigger:
      - platform: time
        at: "07:00:00"
    action:
      - service: vw_images.update_images
```

> **Note:** Replace `binary_sensor.your_vw_door_lock` and `sensor.your_vw_charging_state` with the actual entity IDs from your setup. These vary depending on your vehicle integration (e.g., WeConnect, Volkswagen We Connect ID). The short delay before the service call ensures the VW servers have time to update the vehicle status.

## Requirements

- Home Assistant 2024.1 or newer
- A Volkswagen WeConnect account with at least one vehicle
- The [weconnect](https://pypi.org/project/weconnect/) Python library (installed automatically)

## Built with

This integration was developed with [Claude Code](https://claude.ai/claude-code) by Anthropic.

## Credits & Sources

- [weconnect](https://github.com/tillsteinbach/WeConnect-python) – Python library for the Volkswagen WeConnect API by Till Steinbach
- [Home Assistant Developer Documentation](https://developers.home-assistant.io/) – Integration architecture, config flows, coordinators, and entity patterns
- [Home Assistant Core](https://github.com/home-assistant/core) – Reference implementations for ImageEntity, ButtonEntity, and DataUpdateCoordinator

## License

MIT License
