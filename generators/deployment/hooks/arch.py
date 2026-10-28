# Deployment hook for Arch Linux. pkggen doesn't sign pacman repositories, so
# `gpg-key` and `rsa-key` are errors here.
SIGNING = None
OPTIONS = ()


def check(job, errors):
    pass


def sign(job, folder, published):
    return []
