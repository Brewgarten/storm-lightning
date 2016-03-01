"""
Management of cloud images
"""
import json
import logging
import os
import urllib2

import pkg_resources


log = logging.getLogger(__name__)

DEFAULT_IMAGE_CACHE = "/var/cache/cloud-images"

class CloudImagesManager(object):
    """
    Cloud images manager
    """
    def __init__(self):
        self.config = json.loads(pkg_resources.resource_string(__package__, "data/cloud-images.json"))  # @UndefinedVariable)
        if not os.path.exists(DEFAULT_IMAGE_CACHE):
            os.makedirs(DEFAULT_IMAGE_CACHE, 0777)

    def getImage(self, imageType, version="latest", force=False):
        """
        Get image path. Note that this implicitly downloads the image
        into the cache.

        :param imageType: image type
        :type imageType: str
        :param version: version
        :type version: str
        :param force: remove previously cached image
        :type force: bool
        :returns: full image path
        :rtype: str
        """
        link = self.getImageLink(imageType, version=version)
        if not link:
            log.error("link for image type '%s' not found", imageType)
            return None

        fullImageFilePath = os.path.join(DEFAULT_IMAGE_CACHE, os.path.basename(link))
        if force and os.path.exists(fullImageFilePath):
            os.remove(fullImageFilePath)

        # check if we need to retrieve the image
        if not os.path.exists(fullImageFilePath):
            response = urllib2.urlopen(link)
            totalSize = int(response.info().getheader("Content-Length").strip())
            totalSizeInMB = (totalSize / (1024 * 1024)) + 1
            currentSizeInMB = 0
            chunkSize = 1024 * 1024
            with open(fullImageFilePath, "wb") as imageFile:
                while True:
                    chunk = response.read(chunkSize)
                    if not chunk:
                        break
                    imageFile.write(chunk)
                    currentSizeInMB += 1
                    progress = 100.0 * currentSizeInMB / totalSizeInMB
                    log.debug("downloaded '%d/%d' MB (%0.2f%%)",
                              currentSizeInMB,
                              totalSizeInMB,
                              progress)
        return fullImageFilePath

    def getTypes(self):
        """
        Get types of images

        :returns: list of types
        :rtype: [str]
        """
        return self.config.keys()

    def getLatestVersion(self, imageType):
        """
        Get latest image version

        :returns: latest image version
        :rtype: str
        """
        if imageType not in self.config:
            log.error("image type '%s' does not exist", imageType)
            return None
        return self.config[imageType]["latest"]

    def getImageLink(self, imageType, version="latest"):
        """
        Get image link

        :param imageType: image type
        :type imageType: str
        :param version: version
        :type version: str
        :returns: image link/url
        :rtype: str
        """
        if imageType not in self.config:
            log.error("image type '%s' does not exist", imageType)
            return None
        if version == "latest":
            version = self.getLatestVersion(imageType)
        return self.config[imageType]["versions"].get(version, None)

    def getVersions(self, imageType):
        """
        Get image versions

        :returns: image versions
        :rtype: [str]
        """
        if imageType not in self.config:
            log.error("image type '%s' does not exist", imageType)
            return []
        return self.config[imageType]["versions"].keys() + ["latest"]
