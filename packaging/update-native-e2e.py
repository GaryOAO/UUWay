#!/usr/bin/python3
"""CI-only upgrade and rollback with real apt/rpm/pacman and a simulated desktop.

Run last in a disposable installation-test container: tiny uuway packages replace
the installed runtime. No network or Wine is needed for the update transaction.
"""
import importlib.util
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile

spec = importlib.util.spec_from_file_location('update', '/usr/lib/uuway/scripts/uuway_update_apply.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)
TARGET = update.installed_target()
FMT = update.TARGETS[TARGET][0]


def run(argv, **kwargs):
    return subprocess.run(list(map(str, argv)), check=True, capture_output=True, text=True, **kwargs)


class Clock:
    value = 0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Runner:
    unhealthy = set()

    def __call__(self, argv, **kwargs):
        if argv[0] == 'timeout':
            inner = argv[argv.index('env') + 1:]
            while inner and '=' in inner[0]:
                inner = inner[1:]
            output = ''
            if inner[:3] == ['systemctl', '--user', 'show-environment']:
                output = ('XDG_SESSION_TYPE=wayland\nXDG_CURRENT_DESKTOP=Hyprland\n'
                          'HYPRLAND_INSTANCE_SIGNATURE=ci-instance\n')
            elif inner[:3] == ['hyprctl', '-j', 'monitors']:
                output = '[{"name":"HEADLESS-1","disabled":false}]'
            elif inner[:4] == ['systemctl', '--user', 'is-active', '--quiet']:
                return subprocess.CompletedProcess(argv, 3, '', '')
            elif inner[:3] == ['systemctl', '--user', 'show']:
                version = update.parse_installed(update.run_command(update.installed_command(TARGET)), TARGET)
                for name in update.SERVICES:
                    active = name != 'uu-native-bridge' or version not in self.unhealthy
                    output += (f'Id={name}.service\nNRestarts=0\n'
                               f'ActiveState={"active" if active else "failed"}\n'
                               'SubState=running\nActiveEnterTimestampMonotonic=500\n\n')
            return subprocess.CompletedProcess(argv, 0, output, '')
        if argv[0] in ('systemctl', 'ip'):
            return subprocess.CompletedProcess(argv, 0, 'default via 10.0.0.1\n' if argv[0] == 'ip' else '', '')
        return update.run_command(argv, **kwargs)


def make_release(work, version):
    directory = work / version
    directory.mkdir()
    tree = directory / 'tree'
    payload = tree / 'usr/lib/uuway'
    payload.mkdir(parents=True)
    (payload / 'fixture').write_text(version + '\n')
    (payload / 'package-info.json').write_text(json.dumps(dict(target=TARGET, version=version, format=FMT, ota=True)))
    package = directory / update.package_name(version, TARGET)
    if FMT == 'deb':
        (tree / 'DEBIAN').mkdir()
        (tree / 'DEBIAN/control').write_text(
            f'Package: uuway\nVersion: {version}\nArchitecture: amd64\nX-UUWay-Target: {TARGET}\n'
            'Maintainer: CI <ci@example.invalid>\nDescription: update fixture\n')
        run(['dpkg-deb', '--root-owner-group', '-b', tree, package])
    elif FMT == 'rpm':
        top = directory / 'rpmbuild'
        top.mkdir()
        specfile = directory / 'fixture.spec'
        specfile.write_text(
            '%global debug_package %{nil}\n%global __os_install_post %{nil}\n'
            f'Name: uuway\nVersion: {version}\nRelease: 1\nDistribution: {TARGET}\n'
            'Summary: update fixture\nLicense: MIT\n%description\nupdate fixture\n'
            f'%install\nmkdir -p %{{buildroot}}/usr/lib\ncp -a {payload} %{{buildroot}}/usr/lib/\n'
            '%files\n/usr/lib/uuway\n')
        run(['rpmbuild', '--define', '_topdir ' + str(top), '-bb', specfile])
        shutil.copyfile(next((top / 'RPMS').rglob('*.rpm')), package)
    else:
        (tree / '.PKGINFO').write_text(
            f'pkgname = uuway\npkgbase = uuway\npkgver = {version}-1\npkgdesc = update fixture\n'
            'url = https://github.com/GaryOAO/UUWay\nbuilddate = 1\n'
            'packager = CI <ci@example.invalid>\nsize = 0\nlicense = MIT\nxdata = pkgtype=pkg\n'
            'arch = x86_64\nprovides = uuway-target-arch\n')
        run(['bsdtar', '--zstd', '-cf', package, '-C', tree, '.PKGINFO', 'usr'])
    (directory / 'SHA256SUMS').write_text(f'{update.sha256_file(package)}  {package.name}\n')
    return directory


def main():
    assert os.geteuid() == 0, 'Use only inside a disposable CI container'
    with tempfile.TemporaryDirectory(prefix='uuway-ota-e2e-') as temporary:
        work = Path(temporary)
        stages = {v: make_release(work, v) for v in ('9.8.0', '9.8.1', '9.8.2')}
        account = pwd.getpwnam('uuway-test')
        runtime = work / 'runtime'
        (runtime / str(account.pw_uid)).mkdir(parents=True)
        (runtime / str(account.pw_uid) / 'bus').touch()
        layout = update.Layout(work / 'state', runtime=runtime, resume_unit=work / 'resume.service')
        layout.prepare()
        clock, runner = Clock(), Runner()
        baseline = dict(units={n: dict(active=True, restarts=0, entered=500) for n in update.SERVICES},
                        processes={}, connected=None)

        def job(version):
            return update.Job(layout, user=account.pw_name, uid=account.pw_uid, home=account.pw_dir,
                              prefix=account.pw_dir + '/prefix', tag='v' + version, target=TARGET,
                              baseline=baseline, run=runner, clock=clock.now, sleep=clock.sleep)

        first = job('9.8.0')
        run(update.install_command(first, stages['9.8.0'] / update.package_name('9.8.0', TARGET)))

        def attempt(version, previous, expected):
            stage = stages[version]
            old = stage / 'previous'
            old.mkdir(exist_ok=True)
            for name in ('SHA256SUMS', update.package_name(previous, TARGET)):
                shutil.copyfile(stages[previous] / name, old / name)
            current = job(version)
            outcome = update.guarded(current, lambda: update.run_update(current, stage))
            assert outcome == expected, (TARGET, version, outcome, update.read_json(layout.result))
            installed = previous if expected == 'rolled_back' else version
            assert update.installed_version(current) == installed
            assert Path('/usr/lib/uuway/fixture').read_text().strip() == installed
            assert update.dpkg_is_clean(current)
            assert not layout.resume_unit.exists()

        # A corrupt old package must stop the transaction before installing anything.
        bad = stages['9.8.1'] / 'previous'
        bad.mkdir()
        for name in ('SHA256SUMS', update.package_name('9.8.0', TARGET)):
            shutil.copyfile(stages['9.8.0'] / name, bad / name)
        (bad / update.package_name('9.8.0', TARGET)).write_bytes(b'corrupt')
        if FMT != 'deb':
            refused = job('9.8.1')
            assert update.run_update(refused, stages['9.8.1']) == 'not_started'
            assert update.installed_version(refused) == '9.8.0'

        attempt('9.8.1', '9.8.0', 'success')
        journal = update.Journal.load(layout.journal)
        rollback = update.job_from_journal(layout, journal, run=runner, clock=clock.now, sleep=clock.sleep)
        assert rollback.target == TARGET
        assert update.run_manual_rollback(rollback) == 'rolled_back'
        assert update.installed_version(rollback) == '9.8.0'
        assert Path('/usr/lib/uuway/fixture').read_text().strip() == '9.8.0'
        attempt('9.8.1', '9.8.0', 'success')
        runner.unhealthy.add('9.8.2')
        attempt('9.8.2', '9.8.1', 'rolled_back')
        print(f'{TARGET}: real package upgrade, manual rollback, unhealthy rollback and Hyprland session passed')


if __name__ == '__main__':
    main()
