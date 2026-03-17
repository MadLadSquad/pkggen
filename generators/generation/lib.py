#!/usr/bin/env python3
# This file contains shared utilities that every generator will use
import hashlib
import sys
import os
import yaml
from tqdm import tqdm
from io import BytesIO

def load_secrets():
    if os.name == 'nt':
        base_dir = os.getenv('APPDATA', os.path.expanduser('~\\AppData\\Roaming'))
    else:
        base_dir = os.getenv('XDG_CONFIG_HOME', os.path.expanduser('~/.config'))
    secrets_file = os.path.join(base_dir, 'pkggen', 'secrets.yaml')
    if not os.path.exists(secrets_file):
        print("\x1b[33mWarning: secrets.yaml was requested but not found. Creating it automatically.\x1b[0m", file=sys.stderr)
        config_dir = os.path.join(base_dir, 'pkggen')
        os.makedirs(config_dir, exist_ok=True)
        with open(secrets_file, 'w') as f:
            f.write('')
    with open(secrets_file, "r") as stream:
        result = yaml.safe_load(stream)
        return result if result is not None else {}

def readinput():
    return sys.stdin.buffer.read()

class TinyError(Exception):
    def __init__(self, message):
        super().__init__("\x1b[31m" + message + "\x1b[0m")
        self.args = (message, )
        self.__traceback__ = None

def calculate_hash(obj, data, algorithm, key):
    if key != "blake2b" or key != "blake2s":
        h = hashlib.new(algorithm, usedforsecurity=False)
    else:
        h = hashlib.blake2b(digest_size=64) if key == "blake2b" else hashlib.blake2s(digest_size=32)
    h.update(data)
    obj[key] = h.hexdigest()

def calculate_hashes(obj, data):
    calculate_hash(obj, data, "md5", "md5")

    calculate_hash(obj, data, "sha1", "sha1")

    calculate_hash(obj, data, "sha224", "sha2-224")
    calculate_hash(obj, data, "sha3_224", "sha3-224")

    calculate_hash(obj, data, "sha256", "sha2-256")
    calculate_hash(obj, data, "sha3_256", "sha3-256")
    
    calculate_hash(obj, data, "sha384", "sha2-384")
    calculate_hash(obj, data, "sha3_384", "sha3-384")

    calculate_hash(obj, data, "sha512", "sha2-512")
    calculate_hash(obj, data, "sha3_512", "sha3-512")

    calculate_hash(obj, data, "blake2b", "blake2b")
    calculate_hash(obj, data, "blake2s", "blake2s")


def download_to_buffer(response, size, url):
    buf = BytesIO()

    with tqdm(total=size if size > 0 else None, unit="B", unit_scale=True, desc=f"Downloading {url}: ") as pbar:
        for chunk in response.iter_content(chunk_size=1024 * 64):
            if not chunk:
                continue
            buf.write(chunk)
            pbar.update(len(chunk))

    return buf
