"""
Copyright (c) IBM 2015-2017. All Rights Reserved.
Project name: storm-lightning
This project is licensed under the MIT License, see LICENSE
"""
from __future__ import print_function

import sys

from setuptools import setup, find_packages

import versioneer


needs_pytest = {"pytest", "test", "ptr", "coverage"}.intersection(sys.argv)
pytest_runner = ["pytest-runner"] if needs_pytest else []

setup(
    author = "IBM",
    author_email = "",
    cmdclass=versioneer.get_cmdclass(),
    description = "Cloud infrastructure drivers",
    entry_points = {
        "console_scripts" : [
            "storm-lightning-local = storm.lightning.manager:main",
        ]
    },
    install_requires = [
        "apache-libcloud >= 1.0.0-rc1",
        "libvirt-python",
        "prettytable",
    ],
    keywords = "python storm cloud",
    license = "MIT",
    name = "storm-lightning",
    packages = find_packages(),
    setup_requires=[] + pytest_runner,
    tests_require=["pytest", "pytest-cov"],
    url = "",
    version = versioneer.get_version(),
)
