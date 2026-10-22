import pytest

from tests.repositories.conftest import files


def test_debian_skeleton(gen_repo, example_config):
    result = gen_repo("debian", example_config["repositories"]["debian"])
    assert result.code == 0, result.err
    assert files(gen_repo.out) == [".gitignore", "conf/distributions", "conf/options", "packages/.gitkeep",
                                   "untitled-desktop-deb.sources"]
    # Unsigned: pkggen signs the repository when it deploys it
    assert (gen_repo.out / "conf" / "distributions").read_bytes() == (
        b"Origin: MadLadSquad\n"
        b"Label: UntitledDesktopDEB\n"
        b"Suite: stable\n"
        b"Codename: trixie\n"
        b"Architectures: amd64 arm64\n"
        b"Components: main\n"
        b"Description: Debian packages for the UntitledDesktop project\n"
        b"\n"
        b"Origin: MadLadSquad\n"
        b"Label: UntitledDesktopDEB\n"
        b"Suite: unstable\n"
        b"Codename: sid\n"
        b"Architectures: amd64 arm64\n"
        b"Components: main\n"
        b"Description: Debian packages for the UntitledDesktop project\n"
        b"\n"
        b"Origin: MadLadSquad\n"
        b"Label: UntitledDesktopDEB\n"
        b"Codename: noble\n"
        b"Architectures: amd64 arm64\n"
        b"Components: main\n"
        b"Description: Debian packages for the UntitledDesktop project\n"
        b"\n"
        b"Origin: MadLadSquad\n"
        b"Label: UntitledDesktopDEB\n"
        b"Codename: resolute\n"
        b"Architectures: amd64 arm64\n"
        b"Components: main\n"
        b"Description: Debian packages for the UntitledDesktop project\n"
    )
    assert (gen_repo.out / "conf" / "options").read_bytes() == b"verbose\nbasedir .\n"
    assert (gen_repo.out / "untitled-desktop-deb.sources").read_bytes() == (
        b"Types: deb\n"
        b"URIs: https://debs.madladsquad.com/\n"
        b"Suites: trixie sid noble resolute\n"
        b"Components: main\n"
        b"Signed-By: /usr/share/keyrings/untitled-desktop.gpg\n"
    )
    assert "db/" in (gen_repo.out / ".gitignore").read_text().splitlines()


def test_defaults_and_per_suite_overrides(gen_repo):
    block = {"origin": "Me", "suites": [
        {"codename": "sid", "architectures": ["riscv64"], "components": ["main", "contrib"], "description": "Sid"},
        {"codename": "trixie"},
    ]}
    assert gen_repo("debian", block).code == 0
    stanzas = (gen_repo.out / "conf" / "distributions").read_text().split("\n\n")
    assert stanzas[0].splitlines() == ["Origin: Me", "Label: Me", "Codename: sid", "Architectures: riscv64",
                                       "Components: main contrib", "Description: Sid"]
    assert stanzas[1].splitlines() == ["Origin: Me", "Label: Me", "Codename: trixie",
                                       "Architectures: amd64 source", "Components: main"]
    # No url: no .sources file
    assert not list(gen_repo.out.glob("*.sources"))


def test_sources_file_without_keyring(gen_repo):
    assert gen_repo("debian", {"origin": "Me", "name": "me", "url": "https://x/",
                               "suites": [{"codename": "sid"}]}).code == 0
    assert "Signed-By" not in (gen_repo.out / "me.sources").read_text()


@pytest.mark.parametrize("block, message", [
    (None, "pkggen.yaml has no 'repositories.debian' block"),
    ({"suites": [{"codename": "sid"}]}, "'repositories.debian' is missing: origin"),
    ({"origin": "Me"}, "'repositories.debian.suites' needs at least one entry"),
    ({"origin": "Me", "suites": [{"codename": "sid"}, {"suite": "stable"}]},
     "'repositories.debian.suites[1]' needs a 'codename'"),
    ({"origin": "Me", "suites": ["sid"]}, "'repositories.debian.suites[0]' needs a 'codename'"),
    ({"origin": "Me", "url": "https://x/", "suites": [{"codename": "sid"}]},
     "'repositories.debian.name' is required when 'url' is set"),
])
def test_errors(gen_repo, block, message):
    result = gen_repo("debian", block, config={} if block is None else None)
    assert result.code == 1
    assert message in result.err_plain
