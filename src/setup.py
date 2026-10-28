import os
import re
import shutil

from setuptools import setup, find_packages
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

HERE = os.path.dirname(os.path.abspath(__file__))
# The generators live next to src/ in a checkout, and inside an sdist
GENERATORS = [os.path.join(HERE, os.pardir, "generators"), os.path.join(HERE, "generators")]
IGNORE = shutil.ignore_patterns("__pycache__", "*.py[cod]", ".gitkeep")


def generators():
    for path in GENERATORS:
        if os.path.isfile(os.path.join(path, "distributions", "distributions.yaml")):
            return path
    raise RuntimeError("can't find the generators/ folder to bundle with pkggen")


class BuildWithGenerators(build_py):
    """Ships the generators inside the package, as pkggen/generators."""

    def run(self):
        super().run()
        if not self.editable_mode:  # editable installs use the checkout's generators/
            target = os.path.join(self.build_lib, "pkggen", "generators")
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(generators(), target, ignore=IGNORE)


class SdistWithGenerators(sdist):
    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        shutil.copytree(generators(), os.path.join(base_dir, "generators"), ignore=IGNORE)


def version():
    with open(os.path.join(HERE, "pkggen", "version.py"), encoding="utf-8") as f:
        return re.search(r'PKGGEN_VERSION\s*=\s*"([^"]+)"', f.read()).group(1)


setup(
    name="pkggen",
    version=version(),
    description="Automatically generate desktop packages for multiple distributions",
    license="MIT",
    python_requires=">=3.10",
    packages=find_packages(include=["pkggen", "pkggen.*"]),
    package_data={"pkggen": ["pkggen"]},
    install_requires=[
        # Signing Alpine and Void repositories (RSA) when deploying
        "cryptography>=42",
        "Jinja2>=3.1",
        "packaging>=23.0",
        "PyYAML>=6.0",
        "requests>=2.31",
        "tqdm>=4.66",
    ],
    entry_points={
        "console_scripts": [
            "pkggen=pkggen.cli:main",
        ],
    },
    cmdclass={"build_py": BuildWithGenerators, "sdist": SdistWithGenerators},
)
