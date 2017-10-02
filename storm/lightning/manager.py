#!/usr/bin/env python

"""
Copyright (c) IBM 2015-2017. All Rights Reserved.
Project name: storm-lightning
This project is licensed under the MIT License, see LICENSE

This library provides management functionality for virtual machines using ``libvirt``.

"""

import datetime
import logging
import os
import re
import shlex
import shutil
import string
import subprocess
import tempfile
import time

import libvirt
import pkg_resources

from storm.lightning.base import (CDROMInfo,
                                  DiskInfo, DomainInfo,
                                  NetworkInfo, NetworkInterfaceInfo,
                                  StaticHostInfo, StoragePoolInfo, StorageVolumeInfo)
from storm.lightning.utils import waitForSSHAvailable


log = logging.getLogger(__name__)

GIGABYTE = 1024 ** 3

# disable error logging on libvirt
def libvirtCallback(ignore, err):
    """
    Error handler callback
    """
    if err[3] != libvirt.VIR_ERR_ERROR:
        # Don't log libvirt errors: global error handler will do that
        log.warn("Non-error from libvirt: '%s'", err[2])
libvirt.registerErrorHandler(f=libvirtCallback, ctx=None)

class LibVirtManager(object):
    """
    A utility class to manage virtual machine components using ``libvirt``

    :param uri: hypervisor uri
    :type uri: str
    """
    def __init__(self, uri="qemu:///system"):
        self.connection = libvirt.open(uri)

    def addDisk(self, name, storageVolume=None, device=None):
        """
        Add a disk to the specified domain

        :param name: name of the domain
        :type name: str
        :param storageVolume: storage volume
        :type storageVolume: :class:`~libvirt.virStorageVol`
        :param device: target device, e.g., ``sdb``
        :type device: str
        """
        # TODO: instance checks for parameters
        availableDevices = self.getAvailableDeviceLetters(name)

        if storageVolume is None:
            # create a new storage volume
            storageVolumes = self.getStorageVolumesByDomain(name)
            storagePoolName = storageVolumes[0].storagePoolLookupByVolume().name()
            storageVolume = self.createStorageVolume(storagePoolName, "{0}-{1}".format(name, availableDevices[0]))

        if device is None:
            device = "sd{0}".format(availableDevices[0])
        # TODO: investigate listing of devices because Linux autosorts them

        # define disk interface
        diskInfo = DiskInfo(storageVolume.path(), device.replace("/dev/", ""))
        log.debug(diskInfo.toXML(pretty=True))

        # attach disk to guest
        domain = self.getDomainByName(name)
        domain.attachDeviceFlags(diskInfo.toXML(), libvirt.VIR_DOMAIN_AFFECT_CONFIG)

    # TODO: define Units enum
    def createDomain(self, name, imagePath, poolName, networkName, memory=2048, cpus=2):
        """
        Create a new guest using the specified storage pool and network names

        :param name: name of the new guest
        :type name: str
        :param imagePath: path to cloud image
        :type imagePath: str
        :param poolName: name of an existing storage pool for this guest
        :type poolName: str
        :param networkName: name of an existing network for this guest
        :type networkName: str
        :param memory: memory in MB
        :type memory: int
        :param cpus: number of cpus
        :type cpus: int
        :returns: :class:`~libvirt.virDomain`
        """
        if not os.path.exists(imagePath):
            log.error("could not find image '%s'", imagePath)
            return None

        domainInfo = DomainInfo(name, cpus, int(memory * 1024))

        # get all disks in the storage pool
        diskInfos = []
        storagePoolInfo = self.getStoragePoolInfoByName(poolName)
        storagePool = self.getStoragePoolByName(poolName)
        storageVolumes = storagePool.listAllVolumes()
        for storageVolume in storageVolumes:
            # storage volume assumes pattern: nodeName-device
            # e.g., node1-a
            info = storageVolume.name().split("-")
            storageVolumeNode = "-".join(info[:-1])
            storageVolumeDevice = info[-1]

            if storageVolumeNode == name:
                diskInfos.append(DiskInfo(storageVolume.path(), "sd{0}".format(storageVolumeDevice)))

        if not diskInfos:
            log.error("could not create domain '%s' because no storage volumes exist", name)
            return None

        # make sure disk infos are sorted by device name
        diskInfos = sorted(diskInfos, key=lambda diskInfo: diskInfo.dev)

        # copy cloud image into the pool
        osDiskPath = diskInfos[0].path
        os.remove(osDiskPath)
        shutil.copy(imagePath, osDiskPath)
        os.chmod(osDiskPath, 0777)

        # add network interface
        networkInterface = NetworkInterfaceInfo(networkName)
        domainInfo.addDevice(networkInterface)

        # add disks
        for diskInfo in diskInfos:
            domainInfo.addDevice(diskInfo)

        # generate init iso with cloud-init information
        temporaryDirectory = os.path.abspath(tempfile.mkdtemp(prefix="storm-lightning-"))
        os.chmod(temporaryDirectory, 0777)

        userDataPath = os.path.join(temporaryDirectory, "user-data")
        with open(userDataPath, "w") as userDataFile:
            userData = pkg_resources.resource_string(__package__, "data/cloud-init/user-data")  # @UndefinedVariable
            userDataFile.write(userData)
        metaDataPath = os.path.join(temporaryDirectory, "meta-data")
        with open(metaDataPath, "w") as metaDataFile:
            metaData = pkg_resources.resource_string(__package__, "data/cloud-init/meta-data")  # @UndefinedVariable
            metaData = metaData.replace("<name>", name)
            metaDataFile.write(metaData)
        initIsoPath = os.path.join(storagePoolInfo.path, "{name}-init.iso".format(name=name))

        genisoCommand = "genisoimage -quiet -output {initIso} -volid cidata -joliet -rock {userData} {metaData}".format(
            initIso=initIsoPath,
            userData=userDataPath,
            metaData=metaDataPath
        )
        subprocess.check_output(shlex.split(genisoCommand))
        shutil.rmtree(temporaryDirectory)

        # add init iso as cdrom
        cdromInfo = CDROMInfo(initIsoPath)
        domainInfo.addDevice(cdromInfo)

        # create domain
        log.debug(domainInfo.toXML(pretty=True))
        domain = self.connection.defineXML(domainInfo.toXML())

        # add static host entry to network
        ipAddress = self.createStaticHost(networkName, networkInterface.mac, name)

        start = datetime.datetime.utcnow()
        domain.create()
        # give domain some time to start
        while domain.info()[0] != libvirt.VIR_DOMAIN_RUNNING:
            time.sleep(1)

        available = waitForSSHAvailable(ipAddress)

        end = datetime.datetime.utcnow()

        if available:
            log.info("creating '%s' took %s", name, end-start)
            return domain
        else:
            log.error("could not create '%s' because contacting the domain timed out", name)
            return None

    def createNetwork(self, name, subnetPrefix, enableNAT=True):
        """
        Create a new network using the specified subnet prefix

        :param name: name of the new network
        :type name: str
        :param subnetPrefix: subnet prefix, e.g., ``10.76``
        :type subnetPrefix: str
        :param enableNAT: enable NAT such that VM can access host network/Internet
        :type enableNAT: bool
        :returns: :class:`~libvirt.virNetwork`
        """
        try:
            networkInfo = NetworkInfo(name, subnetPrefix)
            # TODO: handle non-NAT version
#             if enableNAT:
#                 network.forward.mode = "nat"
            network = self.connection.networkDefineXML(networkInfo.toXML())
            network.create()
            network.setAutostart(True)
            return network
        except (libvirt.libvirtError, ValueError) as error:
            log.debug(error)
        return None

    def createStaticHost(self, networkName, mac, hostname, ip=None):
        """
        Create static host for the specified host. Note that if no IP address
        is given one is chosen automatically from the networks DHCP range

        :param networkName: name of the network
        :type networkName: str
        :param mac: MAC address
        :type mac: str
        :param hostname: host name
        :type hostname: str
        :param ip: IP address
        :type ip: str
        :returns: assigned IP address
        """
        try:
            networkInfo = self.getNetworkInfoByName(networkName)
            usedIps = set()
            for staticHost in networkInfo.staticHosts:
                if staticHost.mac == mac:
                    log.error("'%s' already has entry with matching MAC: Hostname '%s' MAC '%s' and IP '%s'",
                              networkName, staticHost.name, staticHost.mac, staticHost.ip)
                    return

                if staticHost.name == hostname:
                    log.error("'%s' already has entry with matching hostname: Hostname '%s' MAC '%s' and IP '%s'",
                              networkName, staticHost.name, staticHost.mac, staticHost.ip)
                    return

                if ip and staticHost.ip == ip:
                    log.error("'%s' already has entry with matching IP: Hostname '%s' MAC '%s' and IP '%s'",
                              networkName, staticHost.name, staticHost.mac, staticHost.ip)
                    return

                usedIps.add(staticHost.ip)

            if not ip:
                # find available ip address
                ipStart, ipEnd = networkInfo.dhcpRange
                ipStartInfo = ipStart.split(".")
                ipEndInfo = ipEnd.split(".")
                subnetPrefix = ".".join(ipStartInfo[:-1])

                for hostIp in range(int(ipStartInfo[-1]), int(ipEndInfo[-1])+1):
                    potentialIp = "{0}.{1}".format(subnetPrefix, hostIp)
                    if potentialIp not in usedIps:
                        ip = potentialIp
                        break

                if not ip:
                    log.error("no available ip found")
                    return

            host = StaticHostInfo(mac, hostname, ip)
            network = self.getNetworkByName(networkName)
            network.update(
                libvirt.VIR_NETWORK_UPDATE_COMMAND_ADD_LAST,
                libvirt.VIR_NETWORK_SECTION_IP_DHCP_HOST,
                -1,
                host.toXML()
            )

        except libvirt.libvirtError as error:
            log.error(error)
        return ip

    def createStoragePool(self, name, nodes=None, disks=None):
        """
        Create a new storage pool for the nodes with the specified number
        of disks

        :param name: name of the storage pool
        :type name: str
        :param nodes: list of node names
        :type nodes: [str]
        :param disks: list of disk sizes (in GB)
        :type disks: [int]
        :type start: int
        :returns: :class:`~libvirt.virStoragePool`
        """
        try:
            storagePoolInfo = StoragePoolInfo(name)
            pool = self.connection.storagePoolDefineXML(storagePoolInfo.toXML(), 0)
            pool.build(flags=libvirt.VIR_STORAGE_POOL_BUILD_NEW)
            pool.create()
            pool.setAutostart(True)

            if nodes is None:
                nodes = []
            if disks is None:
                disks = []

            # add storage volumes for the nodes
            if isinstance(nodes, str):
                nodes = [nodes]
            for node in nodes:
                self.createStorageVolumes(name, node, disks)
            return pool
        except (libvirt.libvirtError, ValueError) as error:
            log.debug(error)
        return None

    def createStorageVolume(self, poolName, name, capacity=(10 * GIGABYTE)):
        """
        Create a new storage volume in the specified pool of disks

        :param poolName: name of the storage pool
        :type poolName: str
        :param name: name of the storage volume
        :type name: str
        :param capacity: capacity in bytes
        :type capacity: int
        :rtype: :class:`~libvirt.virStorageVol`
        """
        try:
            storagePool = self.getStoragePoolByName(poolName)
            if storagePool:
                storageVolumeInfo = StorageVolumeInfo(name, capacity)
                volume = storagePool.createXML(storageVolumeInfo.toXML(),
                                               flags=libvirt.VIR_STORAGE_VOL_CREATE_PREALLOC_METADATA)
                return volume
            else:
                log.error("Could not find storage pool '%s'", poolName)
        except (libvirt.libvirtError, ValueError) as error:
            log.error(error)
        return None

    def createStorageVolumes(self, poolName, node, disks):
        """
        Create multiple storage volumes in the specified pool of disks

        :param poolName: name of the storage pool
        :type poolName: str
        :param name: name of the storage volume
        :type name: str
        :param disks: list of disk sizes
        :type disks: [int]
        :type start: int
        :returns: [:class:`~libvirt.virStorageVol`]
        """
        storageVolumes = []
        for diskNumber, diskSize in enumerate(disks):
            diskSizeInGB = diskSize * GIGABYTE
            storageVolume = self.createStorageVolume(poolName,
                                                     "{0}-{1}".format(node, string.ascii_lowercase[diskNumber]),
                                                     capacity=diskSizeInGB)
            if storageVolume is not None:
                storageVolumes.append(storageVolume)
        return storageVolumes

    def deleteDomain(self, name):
        """
        Delete domain and its associated storage volumes

        :param name: name of the domain
        :type name: str
        """
        domain = self.getDomainByName(name)
        if not domain:
            return
        storageVolumes = self.getStorageVolumesByDomain(name)
        networkInfos = self.getNetworkInfosByDomain(name)

        if not domain.isActive():
            try:
                domain.create()
            except libvirt.libvirtError:
                domain.undefine()

        try:
            log.debug("Attempting to destroy '%s'", domain.name())
            domain.destroy()
        except libvirt.libvirtError as error:
            log.debug(error)
        try:
            domain.undefine()
        except libvirt.libvirtError as error:
            log.debug(error)

        if storageVolumes:
            storagePool = storageVolumes[0].storagePoolLookupByVolume()
            storagePoolName = storagePool.name()
            for storageVolume in storageVolumes:
                storageVolume.delete()

            # remove cloud init iso
            storagePoolInfo = self.getStoragePoolInfoByName(storagePoolName)
            initIsoPath = os.path.join(storagePoolInfo.path, "{name}-init.iso".format(name=name))
            cloudInitStorageVolume = self.getStorageVolumeByPath(initIsoPath)
            if cloudInitStorageVolume:
                cloudInitStorageVolume.delete()
            elif os.path.exists(initIsoPath):
                os.remove(initIsoPath)

            # check if storage pool is now empty and can be deleted
            if not storagePool.listAllVolumes() or not os.listdir(storagePoolInfo.path):
                self.deleteStoragePool(storagePoolName)

        if networkInfos:
            networkName = networkInfos[0].name
            self.deleteStaticHost(networkName, name)
            # check if network is now empty and can be deleted
            networkInfo = self.getNetworkInfoByName(networkName)
            if not networkInfo.staticHosts:
                self.deleteNetwork(networkName)

    def deleteNetwork(self, name):
        """
        Delete network

        :param name: name of the network
        :type name: str
        """
        try:
            network = self.connection.networkLookupByName(name)

            if not network.isActive():
                network.create()

            network.destroy()
            network.undefine()
        except libvirt.libvirtError as error:
            if not re.search('not found', error.get_error_message()):
                log.error(error)

    def deleteStaticHost(self, networkName, hostname):
        """
        Delete static host for the host

        :param networkName: name of the network
        :type networkName: str
        :param hostname: host name
        :type hostname: str
        """
        try:
            networkInfo = self.getNetworkInfoByName(networkName)
            hostInfo = networkInfo.getStaticHostInfoByName(hostname)
            if not hostInfo:
                log.debug("static host '%s' does not exist in '%s'", hostname, networkName)
                return

            network = self.getNetworkByName(networkName)
            network.update(
                libvirt.VIR_NETWORK_UPDATE_COMMAND_DELETE,
                libvirt.VIR_NETWORK_SECTION_IP_DHCP_HOST,
                -1,
                hostInfo.toXML()
            )
        except libvirt.libvirtError as error:
            if not re.search('not found', error.get_error_message()):
                log.error(error)

    def deleteStoragePool(self, name):
        """
        Delete storage pool and its associated storage volumes

        :param name: name of the storage pool
        :type name: str
        """
        try:
            storagePool = self.connection.storagePoolLookupByName(name)

            if not storagePool.isActive():
                storagePool.create()

            storageVolumes = storagePool.listAllVolumes()
            for storageVolume in storageVolumes:
                storageVolume.delete()
            storagePool.destroy()
            storagePool.delete()
            storagePool.undefine()
        except libvirt.libvirtError as error:
            if not re.search('not found', error.get_error_message()):
                log.error(error)

    def deleteStorageVolume(self, poolName, name):
        """
        Delete storage volume in the specified pool

        :param poolName: name of the storage pool
        :type poolName: str
        :param name: name of the storage volume
        :type name: str
        """
        try:
            storagePool = self.getStoragePoolByName(poolName)
            storageVolume = storagePool.storageVolLookupByName(name)
            storageVolume.delete()
        except libvirt.libvirtError as error:
            log.error(error)

    def getAvailableDeviceLetters(self, name):
        """
        Get available device letters. E.g., for a domain with two disk
        devices ["c", "d", ...]

        :param name: name of the domain
        :type name: str
        :returns: [str]
        """
        availableDevices = string.ascii_lowercase
        storageVolumes = self.getStorageVolumesByDomain(name)
        for existingStorageVolume in storageVolumes:
            availableDevices = availableDevices.replace(existingStorageVolume.name().split("-")[-1], "")
        return list(availableDevices)

    def getAvailableSubnetPrefixes(self):
        """
        Get available subnet prefixes

        :returns: [str]
        """
        usedSubnetPrefixes = set([
            ".".join(networkInfos.ipAddress.split(".")[:2])
            for networkInfos in self.getNetworkInfos()
        ])
        return [
            "10.{0}".format(part)
            for part in range(50, 200)
            if "10.{0}".format(part) not in usedSubnetPrefixes
        ]

    def getDomainByName(self, name):
        """
        Get domain

        :param name: name of the domain
        :type name: str
        :returns: :class:`~libvirt.virDomain` or ``None``
        """
        try:
            return self.connection.lookupByName(name)
        except libvirt.libvirtError as error:
            if not re.search('not found', error.get_error_message()):
                log.error(error)
        return None

    def getDomainInfoByName(self, name):
        """
        Get domain info

        :param name: name of the domain
        :type name: str
        :returns: :class:`~DomainInfo` or ``None``
        """
        domain = self.getDomainByName(name)
        if domain:
            return DomainInfo.fromXML(domain.XMLDesc())
        return None

    def getDomainInfos(self):
        """
        Get domain infos

        :returns: [:class:`~DomainInfo`]
        """
        return [
            DomainInfo.fromXML(domain.XMLDesc())
            for domain in self.getDomains()
        ]

    def getDomainNames(self):
        """
        Get domain names

        :returns: [str]
        """
        try:
            return self.connection.listDefinedDomains()
        except libvirt.libvirtError as error:
            log.error(error)
        return []

    def getDomains(self):
        """
        Return a list of all domains

        :returns: [:class:`~libvirt.virDomain`]
        """
        try:
            return self.connection.listAllDomains()
        except libvirt.libvirtError as error:
            if not re.search('not found', error.get_error_message()):
                log.error(error)
        return []

    def getNetworkByName(self, name):
        """
        Get network

        :param name: name of the network
        :type name: str
        :returns: :class:`~libvirt.virNetwork` or ``None``
        """
        try:
            return self.connection.networkLookupByName(name)
        except libvirt.libvirtError as error:
            if not re.search('not found', error.get_error_message()):
                log.error(error)
        return None

    def getNetworks(self):
        """
        Return a list of all networkds

        :returns: [:class:`~libvirt.virNetwork`]
        """
        try:
            return self.connection.listAllNetworks()
        except libvirt.libvirtError as error:
            if not re.search('not found', error.get_error_message()):
                log.error(error)
        return []

    def getNetworksByDomain(self, name):
        """
        Get networks for the domain

        :param name: name of the domain
        :type name: str
        :returns: [:class:`~libvirt.virNetwork`]
        """
        networks = []
        domainInfo = self.getDomainInfoByName(name)
        if domainInfo:
            for networkInterfaceInfo in domainInfo.networkInterfaceInfos:
                network = self.getNetworkByName(networkInterfaceInfo.network)
                if network:
                    networks.append(network)
        return networks

    def getNetworkInfoByName(self, name):
        """
        Get network info

        :param name: name of the network
        :type name: str
        :returns: :class:`~NetworkInfo` or ``None``
        """
        network = self.getNetworkByName(name)
        if network:
            return NetworkInfo.fromXML(network.XMLDesc())
        return None

    def getNetworkInfos(self):
        """
        Get networks infos

        :returns: [:class:`~NetworkInfo`]
        """
        return [
            NetworkInfo.fromXML(network.XMLDesc())
            for network in self.getNetworks()
        ]

    def getNetworkInfosByDomain(self, name):
        """
        Get networks info for the domain

        :param name: name of the domain
        :type name: str
        :returns: [:class:`~NetworkInfo`]
        """
        return [
            NetworkInfo.fromXML(network.XMLDesc())
            for network in self.getNetworksByDomain(name)
        ]

    def getNetworkNames(self):
        """
        Get network names

        :returns: [str]
        """
        try:
            return self.connection.listNetworks()
        except libvirt.libvirtError as error:
            log.error(error)
        return []

    def getStaticHostInfoByName(self, networkName, hostname):
        """
        Get static host info

        :param networkName: name of the network
        :type networkName: str
        :param hostname: host name
        :type hostname: str
        :returns: :class:`~StaticHostInfo` or ``None``
        """
        networkInfo = self.getNetworkInfoByName(networkName)
        if networkInfo:
            return networkInfo.getStaticHostInfoByName(hostname)
        return None

    def getStoragePoolByName(self, name):
        """
        Get storage pool

        :param name: name of the storage pool
        :type name: str
        :returns: :class:`~libvirt.virStoragePool` or ``None``
        """
        try:
            return self.connection.storagePoolLookupByName(name)
        except libvirt.libvirtError as error:
            if not re.search('not found', error.get_error_message()):
                log.error(error)
        return None

    def getStoragePoolInfoByName(self, name):
        """
        Get storage pool info

        :param name: name of the storage pool
        :type name: str
        :returns: :class:`~StoragePoolInfo` or ``None``
        """
        storagePool = self.getStoragePoolByName(name)
        if storagePool:
            return StoragePoolInfo.fromXML(storagePool.XMLDesc())
        return None

    def getStoragePoolNames(self):
        """
        Get storage pool names

        :returns: [str]
        """
        try:
            return self.connection.listStoragePools()
        except libvirt.libvirtError as error:
            log.error(error)
        return []

    def getStorageVolumeByPath(self, path):
        """
        Get storage volume

        :param path: path of the storage volume
        :type path: str
        :returns: :class:`~libvirt.virStorageVol` or ``None``
        """
        try:
            return self.connection.storageVolLookupByPath(path)
        except libvirt.libvirtError as error:
            if not re.search('not found', error.get_error_message()):
                log.error(error)
        return None

    def getStorageVolumeInfoByPath(self, path):
        """
        Get storage volume info

        :param path: path of the storage volume
        :type path: str
        :returns: :class:`~StorageVolumeInfo` or ``None``
        """
        storageVolume = self.getStorageVolumeByPath(path)
        if storageVolume:
            return StorageVolumeInfo.fromXML(storageVolume.XMLDesc())
        return None

    def getStorageVolumesByDomain(self, name):
        """
        Get storage volumes for the domain

        :param name: name of the domain
        :type name: str
        :returns: [:class:`~libvirt.virStorageVol`]
        """
        storageVolumes = []
        domainInfo = self.getDomainInfoByName(name)
        for diskInfo in domainInfo.diskInfos:
            storageVolume = self.getStorageVolumeByPath(diskInfo.path)
            if storageVolume:
                storageVolumes.append(storageVolume)
        return storageVolumes

    def getStorageVolumeInfos(self):
        """
        Return a list of all storage volume infos

        :returns: [:class:`~StorageVolume`]
        """
        try:
            storageVolumeInfos = []
            for name in self.getStoragePoolNames():
                storagePool = self.getStoragePoolByName(name)
                storagePoolInfo = StoragePoolInfo.fromXML(storagePool.XMLDesc())

                for storageVolumeName in storagePool.listVolumes():
                    path = "{0}/{1}".format(storagePoolInfo.path, storageVolumeName)
                    storageVolumeInfos.append(self.getStorageVolumeInfoByPath(path))

            return storageVolumeInfos
        except libvirt.libvirtError as error:
            if not re.search('not found', error.get_error_message()):
                log.error(error)
        return []

    def startDomain(self, name):
        """
        Start domain

        :param name: name of the domain
        :type name: str
        """
        try:
            domain = self.getDomainByName(name)
            if domain:
                domain.create()
        except libvirt.libvirtError as error:
            if not re.search('not found', error.get_error_message()):
                log.error(error)

