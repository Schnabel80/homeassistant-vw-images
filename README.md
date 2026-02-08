# VW Images – Home Assistant Integration

A custom Home Assistant integration that provides vehicle images from your Volkswagen WeConnect account.

## Features

- Displays a vehicle image for each car in your VW WeConnect account
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

### HACS (coming soon)

This integration is not yet available in HACS.

## Usage

### Button

Each vehicle gets an **Update Image** button entity. Press it to refresh the vehicle image from VW WeConnect.

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

## Requirements

- Home Assistant 2024.1 or newer
- A Volkswagen WeConnect account with at least one vehicle
- The [weconnect](https://pypi.org/project/weconnect/) Python library (installed automatically)

## License

MIT License
