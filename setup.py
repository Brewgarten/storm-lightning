from __future__ import print_function

import sys

from setuptools import setup, find_packages

import versioneer


needs_pytest = {"pytest", "test", "ptr", "coverage"}.intersection(sys.argv)
pytest_runner = ["pytest-runner"] if needs_pytest else []

try:
    import guestfs
except ImportError:
    print("Required Python module 'guestfs' is missing, please install using something like 'sudo yum install python-libguestfs'", file=sys.stderr)
    sys.exit(1)

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
    license = "IBM",
    name = "storm-lightning",
    packages = find_packages(),
    setup_requires=[] + pytest_runner,
    tests_require=["pytest", "pytest-cov"],
    url = "",
    version = versioneer.get_version(),
)
