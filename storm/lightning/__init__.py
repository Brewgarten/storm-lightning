import logging
import os
from pkgutil import extend_path
import sys

from ._version import get_versions


__path__ = extend_path(__path__, __name__)

__version__ = get_versions()['version']
del get_versions


log = logging.getLogger(__name__)

LIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")

if os.path.exists(LIB_PATH):
    log.debug("using bundled lib versions from '%s'", LIB_PATH)
    if LIB_PATH not in sys.path:
        sys.path.insert(0, LIB_PATH)
