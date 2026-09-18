"""The System panel's group boxes — one class per concern.

``system_panel.py`` held all six in one 28-method widget, which was qtgui's
only god class and a 65% outlier in its own skin.  This is the same split
``panels/led/`` applies to LED control: a thin host plus focused parts.

* :class:`PlatformBox`    — distro, install method, where files live
* :class:`GpuBox`         — which GPU the metric sources read
* :class:`MaintenanceBox` — autostart, update check, in-app upgrade
* :class:`HealthBox`      — the doctor row + the bug-report bundle
* :class:`SensorsBox`     — live readings, HDD toggle, DRAM slots
* :class:`DashboardBox`   — the sensor grid the LCD overlay reads

:class:`SensorsBox` is the only one with anything on a timer; see its
docstring for why that is a structural decision rather than a convention.
"""
from ._base import SystemBox
from .dashboard_box import DashboardBox
from .gpu_box import GpuBox
from .health_box import HealthBox
from .maintenance_box import MaintenanceBox
from .platform_box import PlatformBox
from .sensors_box import SensorsBox

__all__ = (
    "DashboardBox",
    "GpuBox",
    "HealthBox",
    "MaintenanceBox",
    "PlatformBox",
    "SensorsBox",
    "SystemBox",
)
