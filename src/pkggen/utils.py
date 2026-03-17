#!/usr/bin/env python3
import os
import yaml
import sys

def create_secrets_file():
    if os.name == 'nt':
        base_dir = os.getenv('APPDATA', os.path.expanduser('~\\AppData\\Roaming'))
    else:
        base_dir = os.getenv('XDG_CONFIG_HOME', os.path.expanduser('~/.config'))
    config_dir = os.path.join(base_dir, 'pkggen')
    os.makedirs(config_dir, exist_ok=True)
    secrets_file = os.path.join(config_dir, 'secrets.yaml')
    if not os.path.exists(secrets_file):
        with open(secrets_file, 'w') as f:
            f.write('')
    return secrets_file

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
