"""
Copyright (c) IBM 2015-2017. All Rights Reserved.
Project name: storm-lightning
This project is licensed under the MIT License, see LICENSE

`libcloud` driver based on `libvirt`
"""
import logging
import os
import socket

from libcloud.common.types import LibcloudError
from libcloud.compute.base import NodeDriver, Node, NodeImage, NodeSize, StorageVolume, NodeLocation
from libcloud.compute.base import NodeState
from libcloud.compute.providers import set_driver

from storm.lightning import (DEFAULT_STORAGE_PATH,
                             CloudImagesManager,
                             LibVirtManager,
                             waitForSSHAvailable)


log = logging.getLogger(__name__)



DEFAULT_CPU_SIZE = 2
DEFAULT_RAM_SIZE = 2048
DEFAULT_DISK_SIZE = 10

class LocalNodeDriver(NodeDriver):
    """
    Local node driver using ``libvirt`` to manage ``KVM`` virtual machines

    """
    type = "local"
    name = "LocalNodeDriver"
    features = {"create_node": ["generates_password"]}

    def __init__(self):
        super(LocalNodeDriver, self).__init__("local")
        self.manager = LibVirtManager()
        self.cloudImagesManager = CloudImagesManager()

    def _domain_info_to_node(self, domainInfo):
        """
        Convert domain info into a libcloud Node

        :param domainInfo: guest info
        :type domainInfo: :class:`~DomainInfo`
        :returns: node
        :rtype: :class:`~libcloud.compute.base.Node`
        """
        state = NodeState.STOPPED

        publicIps = []
        networkInfos = self.manager.getNetworkInfosByDomain(domainInfo.name)
        if networkInfos:
            staticHost = self.manager.getStaticHostInfoByName(networkInfos[0].name, domainInfo.name)
            if staticHost:
                publicIps.append(staticHost.ip)

                sshSocket = socket.socket()
                sshSocket.settimeout(0.1)
                try:
                    sshSocket.connect((str(staticHost.ip), 22))
                    sshSocket.recv(256)
                    sshSocket.close()
                    state = NodeState.RUNNING
                except socket.error:
                    pass

        # TODO: size, images, and extra information
        disks = {}
        for diskInfo in domainInfo.diskInfos:
            storageVolume = self.manager.getStorageVolumeInfoByPath(diskInfo.path)
            if storageVolume:
                disks[diskInfo.dev] = storageVolume.capacity / (1024 ** 3)

        extra = {
            "password": "password",
            "disks" : disks
        }
        return Node(domainInfo.uuid,
                    domainInfo.name,
                    state,
                    publicIps,
                    [],
                    self,
                    extra=extra)

    def _storage_volume_info_to_storage_volume(self, storageVolumeInfo):
        """
        Convert storage volume info into a libcloud StorageVolume

        :param storageVolumeInfo: storage volume info
        :type storageVolumeInfo: :class:`~StorageVolumeInfo`
        :returns: storage volume
        :rtype: :class:`~libcloud.compute.base.StorageVolume`
        """
        return StorageVolume("{poolName}/{name}".format(
            poolName=storageVolumeInfo.poolName,
            name=storageVolumeInfo.name),
                             storageVolumeInfo.name,
                             storageVolumeInfo.capacity/(1024*1024*1024),
                             self,
                             extra={"capacity" : storageVolumeInfo.capacity})

    def attach_volume(self, node, volume, device=None):
        """
        Attache volume to node

        :param node: node
        :type node: :class:`~libcloud.compute.base.Node`
        :param volume: volume
        :type volume: :class:`~libcloud.compute.base.StorageVolume`
        :param device: where the device is exposed, e.g. '/dev/sdb'
        :type device: str
        :rytpe: ``bool``
        """
        storageVolume = self.manager.getStorageVolumeByPath(volume.id)
        self.manager.addDisk(node.name, storageVolume=storageVolume, device=device)
        return True

    def create_node(self, **kwargs):
        """
        Create a new node instance. This instance will be started
        automatically.

        :param image: image/template to be used for the node
        :type image: :class:`~libcloud.compute.base.NodeImage`
        :param location: location
        :type location: :class:`~libcloud.compute.base.NodeLocation`
        :param name: node name
        :type name: str
        :param size: size
        :type size: :class:`~libcloud.compute.base.NodeSize`
        :param subnetPrefix: subnet prefix, e.g., ``10.76``
        :type subnetPrefix: str
        :returns: :class:`~libcloud.compute.base.Node`
        """
        if "name" not in kwargs:
            raise ValueError("name needs to be specified")
        name = kwargs["name"]
        if "image" not in kwargs:
            raise ValueError("image needs to be specified")
        image = kwargs["image"]
        log.debug("retrieving image '%s' version '%s'", image.extra["imageType"], image.extra["version"])
        imagePath = self.cloudImagesManager.getImage(image.extra["imageType"], image.extra["version"])
        log.debug("finished retrieving image '%s' version '%s'", image.extra["imageType"], image.extra["version"])

        if "size" not in kwargs:
            raise ValueError("size needs to be specified")
        size = kwargs["size"]
        if "location" in kwargs:
            log.warn("This driver currently does not support different locations")

        subnetPrefix = kwargs.get("subnetPrefix", self.manager.getAvailableSubnetPrefixes()[0])

        log.debug("creating '%s' using '%s' in subnet '%s'", name, image.name, subnetPrefix)

        networkName = "net{0}".format(subnetPrefix)
        poolName = "pool{0}".format(subnetPrefix)
        disks = size.extra.get("disks", [size.disk])

        if self.manager.getNetworkInfoByName(networkName) is None:
            self.manager.createNetwork(networkName, subnetPrefix)
        if self.manager.getStoragePoolInfoByName(poolName) is None:
            self.manager.createStoragePool(poolName)
        self.manager.createStorageVolumes(poolName, name, disks)

        # start node automatically unless specified otherwise
        self.manager.createDomain(name,
                                  imagePath,
                                  poolName,
                                  networkName,
                                  memory=size.ram,
                                  cpus=size.extra.get("cpus", 2))

        return self._domain_info_to_node(self.manager.getDomainInfoByName(name))

    def create_volume(self, size, name, location=None, snapshot=None):
        """
        Create a new volume

        :param size: size of volume in gigabytes
        :type size: int
        :param name: fully qualified '<poolname>/<name>' name of the volume to be created
        :type name: str
        :param location: location (ignored by driver)
        :param snapshot:  snapshot (ignored by driver)

        :returns: :class:`~libcloud.compute.base.StorageVolume`
        """
        names = name.split("/")
        if len(names) != 2:
            log.error("name needs to be fully qualified '<poolname>/<name>'")
            return None

        sizeInBytes = size * 1024 * 1024 * 1024
        storageVolume = self.manager.createStorageVolume(names[0], names[1], capacity=sizeInBytes)
        return StorageVolume(storageVolume.key(),
                             storageVolume.name(),
                             size,
                             self,
                             extra={"capacity" : sizeInBytes})

    def destroy_node(self, node):
        """
        Destroy a node.

        :param node: node
        :type node: :class:`~libcloud.compute.base.Node`
        :returns: ``True`` if the destroy was successful, ``False`` otherwise.
        """
        if node:
            self.manager.deleteDomain(node.name)
            return True
        return False

    def destroy_volume(self, volume):
        """
        Destroy a storage volume.

        :param volume: volume
        :type volume: :class:`~libcloud.compute.base.StorageVolume`
        :returns: ``True`` if the destroy was successful, ``False`` otherwise.
        """
        if volume is None:
            return False
        names = volume.id.split("/")
        if len(names) < 2:
            log.error("name needs to be fully qualified '<poolname>/<name>'")
            return False
        self.manager.deleteStorageVolume(names[-2], names[-1])
        return True

    def ex_destroy_network(self, netName):
        """
        Destroy and undefine a network

        :param netName: name of the network
        :type netName: str
        """
        self.manager.deleteNetwork(netName)

    def ex_destroy_storage_pool(self, poolName):
        """
        Destroy and undefine a storage pool

        :param poolName: name of the pool
        :type poolName: str
        """
        self.manager.deleteStoragePool(poolName)

    def ex_get_image_by_name(self, name):
        """
        Get image by name

        :param name: name
        :type name: str
        :returns: :class:`~libcloud.compute.base.NodeImage`
        """
        for image in self.list_images():
            if image.name == name:
                return image
        return None

    def ex_get_node_by_name(self, name):
        """
        Get node by name

        :param name: name
        :type name: str
        :returns: :class:`~libcloud.compute.base.Node`
        """
        domainInfo = self.manager.getDomainInfoByName(name)
        if domainInfo:
            return self._domain_info_to_node(domainInfo)
        return None

    def ex_get_volume_by_name(self, name):
        """
        Get volume by name

        :param name: fully qualified '<poolname>/<name>' name of the volume
        :type name: str
        :returns: :class:`~libcloud.compute.base.StorageVolume`
        """
        storageVolumeInfo = self.manager.getStorageVolumeInfoByPath(
            os.path.join(DEFAULT_STORAGE_PATH, name)
        )
        if storageVolumeInfo:
            return self._storage_volume_info_to_storage_volume(storageVolumeInfo)
        return None

    def ex_start_node(self, node, wait=True):
        """
        Start the node

        :param node: node
        :type node: :class:`~libcloud.compute.base.Node`
        :param wait: wait for the node to finish starting
        :type wait: bool
        """
        self.manager.startDomain(node.name)
        if wait:
            self.ex_wait_for_ready(node)

    def ex_wait_for_ready(self, node, timeout=600):
        """
        Wait until the node is ready, that is its network interface is up

        :param node: node
        :type node: :class:`~libcloud.compute.base.Node`
        :param timeout: timeout
        :type timeout: int
        """
        if node:
            available = waitForSSHAvailable(node.public_ips[0], timeout=timeout)
            if not available:
                raise LibcloudError(
                    value="Could not start node {0}, timed out after {1} seconds".format(node.name, timeout),
                    driver=self)

    def list_images(self, location=None):
        """
        Get a list of images

        :param location: location (ignored by driver)
        :returns: [:class:`~libcloud.compute.base.NodeImage`]
        """
        images = []
        imageId = 1
        for imageType in sorted(self.cloudImagesManager.getTypes()):
            for version in sorted(self.cloudImagesManager.getVersions(imageType)):
                extra = {
                    "imageType": imageType,
                    "version": version
                }
                images.append(
                    NodeImage(imageId,
                              "{imageType}-{version}".format(imageType=imageType, version=version),
                              self,
                              extra=extra)
                )
                imageId += 1
        return images

    def list_locations(self):
        """
        List data centers for a provider

        :return: list of node location objects
        :rtype: ``list`` of :class:`.NodeLocation`
        """
        return [NodeLocation(1, "local", "US", self)]

    def list_nodes(self):
        """
        Get a list of nodes

        :returns: [:class:`~libcloud.compute.base.Node`]
        """
        nodes = [
            self._domain_info_to_node(domainInfo)
            for domainInfo in self.manager.getDomainInfos()
        ]
        nodes = sorted(nodes, key=lambda node: node.name)
        return nodes

    def list_sizes(self, location=None):
        """
        Get a list of sizes

        :param location: location (ignored by driver)
        :returns: [:class:`~libcloud.compute.base.NodeSize`]
        """
        sizes = []
        # TODO: define more sizes
        sizes.append(NodeSize(1, "1 CPU, 1GB ram, 10GB", 1024, 10, 1000, 0.0, self, extra={"cpus": 1}))
        sizes.append(NodeSize(2, "2 CPU, 2GB ram, 10GB", 2048, 10, 1000, 0.0, self, extra={"cpus": 2}))
        return sizes

    def list_volumes(self):
        """
        Get a list of volumes

        :returns: [:class:`~libcloud.compute.base.StorageVolume`]
        """
        volumes = []
        for storageVolumeInfo in self.manager.getStorageVolumeInfos():
            # limit to user generated volumes of type qcow2
            if storageVolumeInfo.path.startswith(DEFAULT_STORAGE_PATH) and storageVolumeInfo.format == "qcow2":
                volumes.append(self._storage_volume_info_to_storage_volume(storageVolumeInfo))
        return volumes

set_driver(LocalNodeDriver.type, LocalNodeDriver.__module__, LocalNodeDriver.name)
