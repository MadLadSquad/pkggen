from setuptools import setup, find_packages

setup(
    name="pkggen",
    version="0.1.0.0",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "pkggen=pkggen.pkggen:main"
        ],
    },
)
