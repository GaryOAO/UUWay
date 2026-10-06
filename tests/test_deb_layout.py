"""The .deb manifest must cover everything the runtime reads from its root."""
import ast
import importlib.util
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))
try:
    import deb_manifest
finally:
    sys.path.pop(0)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


packager = load("package_uu_native_runtime", "scripts/package-uu-native-runtime.py")
DEST = {source: dest for source, dest, _ in deb_manifest.files()}
ROOTED = ("build/", "config/", "scripts/", "assets/")


def packager_sources():
    """Every repository path package-uu-native-runtime.py copies into a bundle."""
    found = set()
    for value in vars(packager).values():
        if (isinstance(value, dict) and value and all(isinstance(v, str) for v in value.values())
                and any(v.startswith(ROOTED) for v in value.values())):
            found.update(v for v in value.values() if v.startswith(ROOTED))
    return found


def script_closure(entries):
    """Scripts reachable from the entry points by import or by a path literal."""
    scripts = ROOT / "scripts"
    stems = {path.stem.replace("-", "_"): path.name for path in scripts.glob("*.py")}
    names = {path.name for path in scripts.glob("*.py")}

    def references(name):
        source = (scripts / name).read_text()
        found = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                found.update(stems[a.name] for a in node.names if a.name in stems and "-" not in stems[a.name])
            elif isinstance(node, ast.ImportFrom) and node.module in stems:
                found.add(stems[node.module])
        for literal in re.findall(r"""['"]([A-Za-z0-9_\-]+\.py)['"]""", source):
            if literal in names:
                found.add(literal)
        for literal in re.findall(r"scripts/([A-Za-z0-9_\-]+\.py)", source):
            if literal in names:
                found.add(literal)
        found.discard(name)
        return found

    seen, pending = set(), list(entries)
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending.extend(references(name))
    return seen


class ManifestCoverageTests(unittest.TestCase):
    def test_every_bundle_input_is_installed(self):
        missing = sorted(packager_sources() - set(DEST))
        self.assertEqual(missing, [], "package-uu-native-runtime.py reads files the .deb does not install")

    def test_installed_paths_keep_the_repository_shape(self):
        for source in packager_sources():
            self.assertEqual(DEST[source], f"{deb_manifest.LIB}/{source}", source)

    def test_scripts_cover_the_runtime_closure(self):
        entries = {"uu-native-service.py", "uu-native-text-service.py", "uu-native-display-service.py",
                   "uuway_console.py", "uuway_cli.py", "install-uu-native-service.py",
                   "configure-uu-wine-mappings.py", "probe-wayland-portal.py",
                   "package-uu-native-runtime.py", "native_launcher_migration.py"}
        shipped = set(deb_manifest.RUNTIME_SCRIPTS)
        self.assertEqual(sorted(script_closure(entries) - shipped), [])
        self.assertEqual(sorted(shipped - script_closure(entries)), [],
                         "the manifest ships a script nothing reaches")

    def test_service_templates_match_the_unit_names_the_installer_writes(self):
        source = (ROOT / "scripts/install-uu-native-service.py").read_text()
        self.assertIn("('text', 'display', 'bridge')", source)
        self.assertEqual(sorted(deb_manifest.SYSTEMD_TEMPLATES),
                         sorted(f"uu-native-{name}.service.in" for name in ("text", "display", "bridge")))

    def test_helpers_are_exactly_what_build_helpers_produces(self):
        build = (ROOT / "scripts/build-helpers.sh").read_text()
        produced = set(re.findall(r'-o "\$output_dir/([^"]+)"', build))
        self.assertEqual(set(deb_manifest.HELPERS), produced)
        self.assertEqual(set(load("uuway_cli", "scripts/uuway_cli.py").HELPERS), produced)

    def test_every_listed_source_is_a_tracked_or_buildable_path(self):
        for source in DEST:
            self.assertFalse(Path(source).is_absolute(), source)
            self.assertNotIn("..", Path(source).parts, source)

    def test_destinations_are_unique_and_never_ship_a_cuda_stub(self):
        destinations = [dest for _, dest, _ in deb_manifest.files()]
        duplicated = {d for d in destinations if destinations.count(d) > 1}
        self.assertEqual(duplicated, set())
        self.assertFalse([d for d in destinations if "libcuda" in d or "stubs" in d])

    def test_executables_are_marked_executable(self):
        for source, dest, mode in deb_manifest.files():
            name = Path(dest).name
            if name in deb_manifest.EXECUTABLE_ELF or name in deb_manifest.EXECUTABLE_HELPERS:
                self.assertEqual(mode, 0o755, dest)
            elif dest.endswith((".dll", ".exe", ".so", ".conf", ".in", ".rules", ".desktop", ".preferences")):
                self.assertEqual(mode & 0o111, 0, dest)

    def test_scripts_are_executable_exactly_when_they_start_with_a_shebang(self):
        for name in deb_manifest.RUNTIME_SCRIPTS:
            first = (ROOT / "scripts" / name).read_bytes()[:2]
            self.assertEqual(deb_manifest.script_mode(name), 0o755 if first == b"#!" else 0o644, name)
        for command in deb_manifest.COMMANDS.values():
            self.assertEqual(deb_manifest.script_mode(Path(command).name), 0o755, command)


class CommandAndIntegrationTests(unittest.TestCase):
    def test_command_links_resolve_to_shipped_scripts(self):
        for link, target in deb_manifest.COMMANDS.items():
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(link), target))
            self.assertIn(resolved, set(DEST.values()), link)

    def test_cli_expects_the_paths_the_package_installs(self):
        cli = load("uuway_cli", "scripts/uuway_cli.py")
        self.assertEqual(str(cli.SHIPPED_RULE).lstrip("/"),
                         "usr/lib/udev/rules.d/70-uurb-native-input.rules")
        self.assertIn("usr/lib/udev/rules.d/70-uurb-native-input.rules", DEST.values())
        library = cli.fcitx_library().relative_to(cli.ROOT)
        self.assertEqual(f"{deb_manifest.LIB}/{library}", DEST["build/native-ime/libuurb-native-ime.so"])

    def test_desktop_entry_points_at_installed_files(self):
        entry = (ROOT / "packaging/deb/uuway.desktop").read_text()
        exec_line = re.search(r"^Exec=(\S+)", entry, re.M).group(1)
        self.assertIn(exec_line.lstrip("/"), deb_manifest.COMMANDS)
        icon = re.search(r"^Icon=(\S+)", entry, re.M).group(1)
        self.assertIn(f"usr/share/icons/hicolor/scalable/apps/{icon}.svg", DEST.values())
        self.assertIn("StartupWMClass=io.uuway.Console", entry)


class MetadataTests(unittest.TestCase):
    def test_control_template_has_the_fields_stage_deb_fills(self):
        control = (ROOT / "packaging/deb/control.in").read_text()
        for field in ("@VERSION@", "@SIZE@", "@SHLIBS@"):
            self.assertEqual(control.count(field), 1, field)
        self.assertIn("Architecture: amd64", control)
        self.assertNotIn("winehq-stable", re.search(r"^Depends:.*$", control, re.M).group(0),
                         "Wine is not packaged by Ubuntu, so it cannot be a hard dependency")

    def test_units_that_run_from_the_package_are_skipped_once_it_is_gone(self):
        for name in ("text", "display"):
            template = (ROOT / f"systemd/uu-native-{name}.service.in").read_text()
            script = re.search(r'^ExecStart=/usr/bin/python3 "(@ROOT@/scripts/[^"]+)"', template, re.M).group(1)
            self.assertIn(f"\nConditionPathExists={script}\n", template, name)
        bridge = (ROOT / "systemd/uu-native-bridge.service.in").read_text()
        self.assertNotIn("ConditionPathExists", bridge, "the bridge runs from the user's own bundle")

    def test_maintainer_scripts_parse_and_never_touch_a_home_directory(self):
        for name in ("postinst", "prerm", "postrm"):
            path = ROOT / "packaging/deb" / name
            self.assertTrue(path.stat().st_mode & 0o111, name)
            self.assertEqual(subprocess.run(["sh", "-n", str(path)]).returncode, 0, name)
            body = path.read_text()
            self.assertTrue(body.startswith("#!/bin/sh\n"), name)
            self.assertNotRegex(re.sub(r"(?m)^\s*#.*$", "", body), r"/home|\$HOME|~/", name)

    def test_wine_pin_prefers_without_forcing_a_downgrade(self):
        preferences = (ROOT / "config/uuway-wine.preferences").read_text()
        self.assertIn("Pin: version 11.0.*", preferences)
        priority = int(re.search(r"Pin-Priority: (\d+)", preferences).group(1))
        self.assertTrue(500 < priority < 1000)

    def test_the_package_never_changes_apt_or_anything_else_under_etc(self):
        # lintian: package-installs-apt-preferences. The pin is written by `uuway setup`, with consent.
        self.assertEqual([d for d in DEST.values() if d.startswith("etc/")], [])
        stage = load("stage_deb", "packaging/stage-deb.py")
        self.assertEqual(stage.CONFFILES, ())
        self.assertIn(f"{deb_manifest.LIB}/config/uuway-wine.preferences", DEST.values())
        cli = load("uuway_cli", "scripts/uuway_cli.py")
        self.assertEqual(str(cli.WINE_PIN), "/etc/apt/preferences.d/uuway-wine")

    def test_lintian_overrides_name_only_the_binaries_that_ship_as_validated(self):
        text = (ROOT / "packaging/deb/lintian-overrides").read_text()
        tags = [line for line in text.splitlines() if line and not line.startswith("#")]
        self.assertTrue(all(line.startswith("uuway: ") for line in tags))
        self.assertIn("usr/share/lintian/overrides/uuway", DEST.values())
        self.assertFalse([line for line in tags if "no-changelog" in line or "apt-preferences" in line],
                         "real errors must be fixed, not overridden")

    def test_the_generated_changelog_is_valid_debian_format(self):
        import gzip
        import tempfile
        stage = load("stage_deb", "packaging/stage-deb.py")
        with tempfile.TemporaryDirectory() as directory:
            stage.write_changelog(Path(directory), "1.2.3", 1790000000)
            text = gzip.decompress((Path(directory) / "usr/share/doc/uuway/changelog.gz").read_bytes()).decode()
        self.assertRegex(text, r"^uuway \(1\.2\.3\) noble; urgency=medium\n\n  \* .+\n\n -- GaryOAO <[^>]+>  "
                               r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d\d [A-Z][a-z]{2} \d{4} \d\d:\d\d:\d\d \+0000\n$")

    def test_copyright_names_every_third_party_component_that_is_shipped(self):
        copyright_text = (ROOT / "packaging/deb/copyright").read_text()
        for name in ("DXVK", "Lachlan Chen", "AGPL-3+", "Zlib", "nv-codec-headers"):
            self.assertIn(name, copyright_text)
        self.assertIn("usr/share/doc/uuway/copyright", DEST.values())


if __name__ == "__main__":
    unittest.main()
