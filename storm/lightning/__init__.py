"""
Storm lightning local ``libvirt``-based virtual machine tooling
"""
from pkgutil import extend_path

from ._version import get_versions
from .base import DEFAULT_STORAGE_PATH
from .images import CloudImagesManager
from .manager import LibVirtManager
from .utils import waitForSSHAvailable


__path__ = extend_path(__path__, __name__)

__version__ = get_versions()['version']
del get_versions

__all__ = [
    "DEFAULT_STORAGE_PATH",
    "CloudImagesManager",
    "LibVirtManager",
    "waitForSSHAvailable"
]
