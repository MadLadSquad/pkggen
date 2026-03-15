#!/usr/bin/env python3
import os
import yaml

def get_generators_path():
    return os.getenv("PKGGEN_GENERATORS_PATH", os.getcwd())

def get_pkggen_config():
    pkggen_path = os.getenv("PKGGEN_RUN_PATH", os.getcwd())
    pkggen_config = os.path.join(pkggen_path, "pkggen.yaml")

    with open(pkggen_config, "r") as stream:
        return yaml.safe_load(stream)

class GenericError(Exception):
    def __init__(self, message):
        super().__init__("\x1b[31m" + message + "\x1b[0m")
        self.args = (message, )
        self.__traceback__ = None
