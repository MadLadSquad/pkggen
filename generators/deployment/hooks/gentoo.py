# Deployment hook for Gentoo. An overlay is a git repository of ebuilds, which
# portage checks with the Manifest's checksums: there's nothing for pkggen to
# sign, so `gpg-key` and `rsa-key` are errors here.
SIGNING = None
OPTIONS = ()


def check(job, errors):
    pass


def sign(job, folder, published):
    return []
