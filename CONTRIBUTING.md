# Contributing to UUWay

感谢你帮助改进 UUWay。Please open an issue before large changes so that the
scope and compatibility impact can be discussed. Small fixes, documentation
updates, and reproducible compatibility reports can go straight to a pull
request.

## Before you start

Read the [README](README.md) and the [build guide](docs/build.md). UUWay is
currently developed and tested on Ubuntu 24.04, GNOME 46, and Wayland. The
runtime supports NVIDIA NVENC first and AMD VA-API as a fallback; Intel-only
systems are not currently supported.

Do not include UU account information, access tokens, private keys, or
unredacted logs in an issue or pull request. UUWay does not distribute the
official UU Remote installer; do not commit that installer or other proprietary
UU files.

## Development setup

```bash
git clone https://github.com/GaryOAO/UUWay.git
cd UUWay
```

For source builds, install the dependencies listed in [docs/build.md](docs/build.md).
The clean reference build is `packaging/container-build.sh`; the Debian package
wrapper is `packaging/build-deb.sh`.

## Checks before opening a pull request

Run the checks that match your change. The commands below are the same checks
used by the repository's CI workflow:

```bash
/usr/bin/python3 -m pytest tests -q -p no:cacheprovider
shellcheck -S warning install.sh packaging/*.sh packaging/deb/postinst \
  packaging/deb/prerm packaging/deb/postrm \
  scripts/build-amd-runtime.sh scripts/uuway-amd.sh
git diff --check
```

Changes that affect packaging or installation should also be checked with:

```bash
packaging/build-deb.sh --version 0.0.0~local --output-dir out
packaging/test-deb.sh out/uuway_0.0.0~local_amd64.deb
```

Hardware-dependent tests may skip when the required GPU, Wine prefix, or live
Wayland session is unavailable. Explain skipped or unavailable validation in
the pull request.

## Pull requests

- Keep each pull request focused on one change.
- Explain the user-visible behaviour, compatibility impact, and how it was
  tested.
- Update the README or relevant docs when commands, supported hardware, or
  configuration change.
- Keep generated build output, credentials, and local runtime state out of the
  commit.
- Preserve the existing AGPL-3.0 licensing and attribution requirements.

Maintainers may ask for a smaller change, additional logs, or a compatibility
test before merging. A pull request is merged only after the required checks
pass and a maintainer has reviewed it.
