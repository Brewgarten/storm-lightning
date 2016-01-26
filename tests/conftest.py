import logging

from libcloud.compute.providers import get_driver
import pytest


log = logging.getLogger(__name__)
logging.basicConfig(format='%(asctime)s [%(levelname)s] [%(name)s(%(filename)s:%(lineno)d)] - %(message)s', level=logging.INFO)

def getLocalDriver():
    import storm.drivers.local
    cls = get_driver("local")
    try:
        # if we don't have privileges for setting up local driver (libvirt) connection
        # then there is no use include it. E.g. here's an error that libvirt will
        # throw in such cases
        #    libvirtError: authentication failed: Authorization requires authentication but no agent is available.
        # in trying
        return cls()
    except:
        return None

@pytest.fixture(scope="module")
def localDriver():
    """
    Local Cloud driver
    """
    driver = getLocalDriver()
    if driver is None:
        pytest.skip("requires libvirt privileges")
    return driver

def pytest_generate_tests(metafunc):
    if "driver" in metafunc.fixturenames:
        localDriverInstance = getLocalDriver()
        metafunc.parametrize("driver", [
                                pytest.mark.skipif(localDriverInstance is None,
                                                   reason="requires libvirt privileges")(localDriverInstance),
                            ])
