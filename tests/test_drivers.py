from libcloud.compute.base import NodeImage, NodeLocation, NodeSize, StorageVolume, Node


def test_list_images(driver):

    for image in driver.list_images():
        assert isinstance(image, NodeImage)
        assert image.driver is driver

def test_list_locations(driver):

    for location in driver.list_locations():
        assert isinstance(location, NodeLocation)
        assert location.driver is driver

def test_list_nodes(driver):

    for node in driver.list_nodes():
        assert isinstance(node, Node)
        assert node.driver is driver
        assert node.extra["password"]

def test_list_sizes(driver):

    for size in driver.list_sizes():
        assert isinstance(size, NodeSize)
        assert size.driver is driver

def test_list_volumes(driver):

    for volume in driver.list_volumes():
        assert isinstance(volume, StorageVolume)
        assert volume.driver is driver
