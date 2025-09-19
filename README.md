# VDS/NSX Config Script for zPodFactory

This is a config-script for [zPodFactory](https://github.com/zPodFactory/zpodcore) that enables advanced VMware vSphere and NSX-T integration for zPods. It provides automated configuration of VMware Distributed Switches (VDS) on vCenter Server Appliance (vCSA) and prepares and configures NSX-T networking components.

This config-script follows zPodFactory's config-scripts architecture as introduced in [PR #43](https://github.com/zPodFactory/zpodcore/pull/43) and [PR #47](https://github.com/zPodFactory/zpodcore/pull/47).

## Overview

The `vdsnsx` config-script is designed to work with zPodFactory's config-scripts system, which allows execution of custom scripts at different stages of a zPod's lifecycle. This script specifically handles:

- **VDS Configuration** (`zpod_component_add_vcsa.py`): Automates the setup of VMware Distributed Switches on vCSA
- **NSX-T Preparation** (`zpod_component_add_nsx.py`): Prepares and configures NSX-T networking components and hosts

## Usage

### Clone the repository into the `config-scripts` directory

In the `zpodcore` directory, run:
```bash
git clone https://github.com/carsso/vdsnsx.git zpodengine/src/zpodengine/config_scripts/vdsnsx
```

### Set as global default configuration

You can configure this as a default config-script for all new zPods using the `ff_default_config_scripts` setting:

In the `zpodcore` directory, run:
```bash
# Set default config-scripts (comma-separated)
just zcli setting create -n ff_default_config_scripts -v "vdsnsx"
```

