"""
Copyright (c) IBM 2015-2017. All Rights Reserved.
Project name: storm-lightning
This project is licensed under the MIT License, see LICENSE

Utility functionality
"""

import datetime
import logging
import socket
import time


log = logging.getLogger(__name__)

def generateMACAddress():
    """
    Generate a MAC address based on the current time

    :returns: mac address
    :rtype: str
    """
    delta = datetime.datetime.now() - datetime.datetime(2015, 1, 1)
    firstPart = hex(delta.seconds).replace("0x", "").zfill(7)
    secondPart = hex(delta.microseconds).replace("0x", "").zfill(5)
    hexString = firstPart + secondPart
    parts = [
        hexString[i:i+2]
        for i in range(0, len(hexString), 2)
    ]
    return ":".join(parts)

def waitForSSHAvailable(ipAddress, timeout=600):
    """
    Wait until SSH is available, that is its network interface is up

    :param ipAddress: ipAddress
    :type ipAddress: str
    :param timeout: timeout
    :type timeout: int
    :returns: whether SSH is available
    :rtype: bool
    """
    sshSocket = socket.socket()
    sshSocket.settimeout(1)
    start = time.time()
    end = start + timeout
    while time.time() < end:

        try:
            sshSocket.connect((ipAddress, 22))
            sshSocket.recv(256)
            sshSocket.close()
            break
        except socket.error:
            time.sleep(1)

    return time.time() <= end
