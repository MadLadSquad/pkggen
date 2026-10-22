"""Picking and rendering a package's `dependencies` (packaging/lib.py and every packaging generator)."""
import pytest

from tests.support.helpers import load_generator
from tests.support.repology import dep, releases

DEBIAN = ["debian_unstable", "debian_13", "ubuntu_26_04", "ubuntu_24_04"]
RPM = ["fedora_44", "fedora_43", "opensuse_tumbleweed", "openmandriva_rolling"]


@pytest.fixture
def plib():
    return load_generator("packaging", "lib")


def job_for(plib, dist, deps, rp_names, **data):
    return plib.Job({
        "distribution": dist, "package": {"name": "pkg", "version": "1.0", **data.pop("package", {})},
        "repository": data.pop("repository", {}), "repository_path": "/repo", "templates_path": "/t/pkg",
        "default_templates_path": "/d", "releases": rp_names, "dependencies": deps, **data})


def resolve(dist, deps, rp_names, **data):
    generator = load_generator("packaging", dist)
    job = job_for(generator.lib, dist, deps, rp_names, **data)
    deps = generator.lib.dependencies(job, generator.DEPENDENCY_FIELDS, generator.pick, generator.render,
                                      **({"local": generator.local_atom} if dist == "gentoo" else {}))
    if dist == "rpm-based":
        generator.split_families(job, deps)
    return job.context["deps"], job.warnings


def resolve_error(dist, deps, rp_names, **data):
    """The PackagingError message resolving fails with."""
    with pytest.raises(Exception) as info:
        resolve(dist, deps, rp_names, **data)
    assert type(info.value).__name__ == "PackagingError", info.value
    return str(info.value)


# --- Helpers ---------------------------------------------------------------

@pytest.mark.parametrize("version, op, wanted, expected", [
    ("3.4", ">=", "3.4", True), ("3.3.10", ">=", "3.4", False), ("1.24.4", ">=", "1.25", False),
    ("2:1.25", ">=", "1.25", True), ("3.4", "=", "3.4.0", True), ("3.5", "<", "3.5", False),
    ("1.0", ">", "0.9", True), ("1.0", "<=", "1.0", True), ("rolling", ">=", "1", True),
])
def test_satisfies(plib, version, op, wanted, expected):
    assert plib.satisfies(version, op, wanted) is expected


@pytest.mark.parametrize("project, rp, expected", [
    ("glfw", "opensuse_tumbleweed", "glfw"),           # not glfw2
    ("go", "opensuse_tumbleweed", "go"),               # the project's own name, not go1.27
    ("go", "debian_13", "golang-1.24"),                # current, not the legacy golang-1.23
    ("go", "gentoo", "dev-lang/go"),                   # not go-bootstrap
    ("dbus", "fedora_44", "dbus"),                     # not mingw-dbus
    ("pkgconf", "opensuse_tumbleweed", "pkgconf"),     # not the mingw cross compilers
    ("glfw", "opensuse_leap_15_6", "glfw"),            # Leap: one entry per binary package
])
def test_choose_package(plib, project, rp, expected):
    assert plib.choose_package(project, releases(project)[rp])["srcname"] == expected


def test_choose_package_skips_arch_split_packages(plib):
    chosen = plib.choose_package("dbus", releases("dbus")["arch"])
    assert chosen["binname"] == "dbus"
    assert plib.choose_package("x", [{"srcname": "mingw-x", "version": "1"}]) is None


def test_gentoo_prefers_a_released_version_over_the_live_ebuild(plib):
    assert plib.choose_package("go", releases("go")["gentoo"])["version"] == "1.27.3"


@pytest.mark.parametrize("project, rp, suffix, main, dev", [
    ("glfw", "debian_13", "-dev", "libglfw3", "libglfw3-dev"),
    ("dbus", "debian_13", "-dev", "dbus", "libdbus-1-dev"),
    ("freetype", "debian_13", "-dev", "libfreetype6", "libfreetype-dev"),
    ("utfcpp", "debian_13", "-dev", None, "libutfcpp-dev"),            # header-only
    ("glfw", "fedora_44", "-devel", "glfw", "glfw-devel"),
    ("dbus", "opensuse_tumbleweed", "-devel", "dbus-1", "dbus-1-devel"),
    ("glfw", "openmandriva_rolling", "-devel", "lib64glfw", "lib64glfw-devel"),
    ("vulkan-headers", "fedora_44", "-devel", "vulkan-headers", "vulkan-headers"),  # no -devel: the package
    ("cmake", "opensuse_leap_15_6", "-devel", "cmake", None),          # Leap lists no binary packages
])
def test_main_and_dev_packages(plib, project, rp, suffix, main, dev):
    package = plib.choose_package(project, releases(project)[rp])
    assert plib.main_package(package, project) == main
    assert plib.dev_package(package, project, suffix) == dev


# --- Every distribution ------------------------------------------------------

def test_arch(plib):
    deps, warnings = resolve("arch", {
        "build": [dep("cmake"), dep("pkgconf")],
        "libraries": [dep("glfw", ">=", "3.4"), dep("vulkan-loader")],
        "runtime": [dep("xdg-desktop-portal")],
        "development": [dep("dbus")],
    }, ["arch"])
    assert deps == {"makedepends": ["cmake", "pkgconf"],
                    "depends": ["glfw>=3.4", "vulkan-icd-loader", "xdg-desktop-portal", "dbus"]}
    assert warnings == []


def test_gentoo(plib):
    deps, warnings = resolve("gentoo", {
        "build": [dep("cmake")],
        "libraries": [dep("glfw", ">=", "3.4"), dep("libx11", "=", "1.8.13")],
        "runtime": [dep("incus")],
        "development": [dep("dbus", "<", "2")],
    }, ["gentoo"])
    assert deps == {"bdepend": ["dev-build/cmake"],
                    "depend": [">=media-libs/glfw-3.4", "~x11-libs/libX11-1.8.13", "<sys-apps/dbus-2"],
                    "rdepend": ["app-containers/incus"]}
    assert warnings == []


def test_gentoo_versions_must_be_gentoo_versions(plib):
    message = resolve_error("gentoo", {"libraries": [dep("go", ">=", "2:1.25~")]}, ["gentoo"])
    assert "the version of the dependency 'go >= 2:1.25~' isn't a Gentoo version" in message


def test_debian(plib):
    deps, warnings = resolve("debian", {
        "build": [dep("cmake")],
        "libraries": [dep("glfw", ">=", "3.4"), dep("freetype")],
        "runtime": [dep("incus")],
        "development": [dep("dbus", ">", "1.0")],
    }, DEBIAN)
    assert deps == {"build_depends": ["cmake", "libglfw3-dev (>= 3.4)", "libfreetype-dev", "libdbus-1-dev (>> 1.0)"],
                    "depends": ["incus", "libdbus-1-dev (>> 1.0)"]}
    # Ubuntu 24.04 has GLFW 3.3
    assert warnings == ["ubuntu_24_04 has glfw 3.3.10, but pkg needs glfw >= 3.4"]


def test_debian_names_that_differ_become_alternatives(plib):
    deps, warnings = resolve("debian", {"build": [dep("go")]}, ["debian_13", "ubuntu_26_04"])
    assert deps["build_depends"] == ["golang-1.24 | golang-1.26"]


def test_rpm_families_that_agree_share_a_line(plib):
    deps, _ = resolve("rpm-based", {"build": [dep("cmake")], "runtime": [dep("xhost")]}, RPM)
    assert deps == {"build_requires": ["cmake"], "requires": ["xhost"], "by_family": []}


def test_rpm_families_that_differ_get_their_own_blocks(plib):
    deps, warnings = resolve("rpm-based", {
        "libraries": [dep("glfw", ">=", "3.4"), dep("fontconfig")],
        "development": [dep("dbus")],
    }, RPM)
    assert deps["build_requires"] == []
    assert deps["requires"] == []
    assert deps["by_family"] == [
        {"family": "fedora", "condition": "0%{?fedora}",
         "build_requires": ["glfw-devel >= 3.4", "fontconfig-devel", "dbus-devel"], "requires": ["dbus-devel"]},
        {"family": "opensuse", "condition": "0%{?suse_version}",
         "build_requires": ["glfw-devel >= 3.4", "fontconfig-devel", "dbus-1-devel"], "requires": ["dbus-1-devel"]},
        {"family": "openmandriva", "condition": "0%{?mdvver}",
         "build_requires": ["lib64glfw-devel >= 3.4", "libfontconfig-devel", "lib64dbus-1-devel"],
         "requires": ["lib64dbus-1-devel"]},
    ]
    assert warnings == []


def test_rpm_releases_of_one_family_that_differ_use_the_most_common_name(plib):
    deps, warnings = resolve("rpm-based", {"libraries": [dep("libglvnd")]},
                             ["openmandriva_rolling", "openmandriva_6_0"])
    assert deps["build_requires"] == ["lib64glvnd-devel"]
    assert warnings == ["the openmandriva releases call libglvnd lib64glvnd-devel, libglvnd-devel: "
                        "using lib64glvnd-devel for all of them"]


def test_rpm_leap_guesses_devel_packages(plib):
    deps, warnings = resolve("rpm-based", {"libraries": [dep("glfw")]}, ["opensuse_leap_15_6"])
    assert deps["build_requires"] == ["glfw-devel"]
    assert warnings == ["repology doesn't list the packages of glfw in opensuse_leap_15_6: guessed glfw-devel"]


# --- Overrides, local packages, missing ones --------------------------------

def test_overrides_are_used_as_they_are(plib):
    deps, _ = resolve("debian", {
        "build": [dep("go", ">=", "1.25", override="golang-go (>= 2:1.25~)")],
        "libraries": [dep("vulkan-headers", override=False)],
    }, DEBIAN)
    assert deps == {"build_depends": ["golang-go (>= 2:1.25~)"], "depends": []}


def test_local_packages(plib):
    local = {"name": "untitled-exec", "category": "ude-base", "packaged": True}
    assert resolve("gentoo", {"development": [dep("untitled-exec", ">=", "1.0", local=local)]},
                   ["gentoo"])[0]["depend"] == [">=ude-base/untitled-exec-1.0"]
    assert resolve("debian", {"development": [dep("untitled-exec", local=local)]}, DEBIAN)[0] == \
        {"build_depends": ["untitled-exec"], "depends": ["untitled-exec"]}
    assert resolve("rpm-based", {"libraries": [dep("untitled-exec", local=local)]}, RPM)[0]["build_requires"] == \
        ["untitled-exec"]


def test_local_packages_without_a_category_use_the_overlays_first(plib):
    local = {"name": "untitled-exec", "category": None, "packaged": True}
    deps, _ = resolve("gentoo", {"libraries": [dep("untitled-exec", local=local)]}, ["gentoo"],
                      repository={"categories": ["ude-misc", "ude-base"]})
    assert deps["depend"] == ["ude-misc/untitled-exec"]


def test_local_packages_not_packaged_here_warn(plib):
    local = {"name": "abcccid", "category": "app-crypt", "packaged": False}
    _, warnings = resolve("arch", {"runtime": [dep("abcccid", local=local)]}, ["arch"])
    assert warnings == ["the dependency 'abcccid' is one of pkggen.yaml's packages, but it isn't packaged for arch"]


def test_releases_without_the_package_warn(plib):
    deps, warnings = resolve("rpm-based", {"runtime": [dep("incus")]}, RPM)
    assert deps["requires"] == [] and deps["by_family"][0]["requires"] == ["incus"]
    assert warnings == ["incus isn't packaged in openmandriva_rolling (repology)"]


def test_dependencies_no_release_has_fail_once(plib):
    message = resolve_error("debian", {"build": [dep("vulkan-headers"), dep("nope")], "runtime": [dep("xhost")]},
                            DEBIAN)
    assert message.startswith("repology has no package for vulkan-headers, nope, xhost in debian_unstable, ")
    assert "`pkggen repology <name>`" in message and 'debian: "<name>"' in message


def test_no_dependencies(plib):
    for dist, fields in [("arch", {"depends", "makedepends"}), ("gentoo", {"depend", "rdepend", "bdepend"}),
                         ("debian", {"build_depends", "depends"}),
                         ("rpm-based", {"build_requires", "requires", "by_family"})]:
        deps, warnings = resolve(dist, {}, [])
        assert set(deps) == fields and not any(deps.values()) and warnings == []


# --- The default templates ---------------------------------------------------

def with_deps(make_job, dist, deps, rp_names):
    job = make_job(dist)
    job.update(releases=rp_names, dependencies=deps)
    return job


def test_default_pkgbuild(package_with, make_job, tmp_path):
    job = with_deps(make_job, "arch", {"build": [dep("cmake")], "libraries": [dep("glfw", ">=", "3.4")]}, ["arch"])
    job["package"]["arch"] = {"depends": ["glibc"]}
    result = package_with("arch", job)
    assert result.code == 0, result.err
    text = (tmp_path / "pkggen-build" / "arch" / "untitled-cli-parser" / "PKGBUILD").read_text()
    assert "depends=('glfw>=3.4' 'glibc')" in text and "makedepends=('cmake')" in text


def test_default_ebuild(package_with, make_job, tmp_path):
    job = with_deps(make_job, "gentoo", {"build": [dep("cmake")], "libraries": [dep("glfw", ">=", "3.4")],
                                         "runtime": [dep("incus")]}, ["gentoo"])
    job["package"]["gentoo"] = {"category": "dev-libs"}
    result = package_with("gentoo", job)
    assert result.code == 0, result.err
    text = (tmp_path / "pkggen-build" / "gentoo" / "dev-libs" / "untitled-cli-parser"
            / "untitled-cli-parser-1.2.0.ebuild").read_text()
    for line in ['DEPEND=">=media-libs/glfw-3.4"', 'RDEPEND="${DEPEND} app-containers/incus"',
                 'BDEPEND="dev-build/cmake"']:
        assert line in text.splitlines(), line


def test_default_control(package_with, make_job, tmp_path):
    job = with_deps(make_job, "debian", {"libraries": [dep("glfw", ">=", "3.4")], "runtime": [dep("incus")]},
                    ["debian_13"])
    job["package"]["debian"] = {"depends": ["libfoo1"]}
    result = package_with("debian", job)
    assert result.code == 0, result.err
    text = (tmp_path / "pkggen-build" / "debian" / "packages" / "untitled-cli-parser" / "debian" / "control").read_text()
    assert "Build-Depends: debhelper-compat (= 13), libglfw3-dev (>= 3.4)" in text
    assert "Depends: ${shlibs:Depends}, ${misc:Depends}, incus, libfoo1" in text


def test_default_spec(package_with, make_job, tmp_path):
    job = with_deps(make_job, "rpm-based", {"build": [dep("cmake")], "libraries": [dep("glfw", ">=", "3.4")],
                                            "runtime": [dep("xhost")]}, RPM)
    result = package_with("rpm-based", job)
    assert result.code == 0, result.err
    text = (tmp_path / "pkggen-build" / "rpm-based" / "SPECS" / "untitled-cli-parser.spec").read_text()
    assert """ExclusiveArch:  %{?x86_64}%{!?x86_64:x86_64} aarch64

BuildRequires:  cmake
Requires:       xhost
%if 0%{?fedora}
BuildRequires:  glfw-devel >= 3.4
%endif
%if 0%{?suse_version}
BuildRequires:  glfw-devel >= 3.4
%endif
%if 0%{?mdvver}
BuildRequires:  lib64glfw-devel >= 3.4
%endif

%description""" in text
