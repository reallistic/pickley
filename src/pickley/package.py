import os
import time
import zipfile

import runez

from pickley import __version__, system
from pickley.context import ImplementationMap
from pickley.delivery import DELIVERERS
from pickley.lock import SoftLock, SoftLockException, vrun
from pickley.pypi import latest_pypi_version, read_entry_points
from pickley.settings import JsonSerializable
from pickley.system import short
from pickley.uninstall import uninstall_existing


PACKAGERS = ImplementationMap("packager")

# These standard locations usually help avoid silly C compilation errors
C_COMPILATION_HELP = {
    "CPPFLAGS": " -I/usr/local/opt/openssl/include",
    "LDFLAGS": " -L/usr/local/opt/openssl/lib",
    "PKG_CONFIG_PATH": ":/usr/local/opt/openssl/lib/pkgconfig",
}


def find_prefix(prefixes, text):
    """
    :param dict prefixes: Prefixes available
    :param str text: Text to examine
    :return str|None: Longest prefix found
    """
    if not text or not prefixes:
        return None
    candidate = None
    for name in prefixes:
        if name and text.startswith(name):
            if not candidate or len(name) > len(candidate):
                candidate = name
    return candidate


class VersionMeta(JsonSerializable):
    """
    Version meta on a given package
    """

    # Dields starting with '_' are not stored to json file
    _problem = None                 # type: str # Detected problem, if any
    _suffix = None                  # type: str # Suffix of json file where this object is persisted
    _name = None                    # type: str # Associated pypi package name

    # Main info, should be passed from latest -> current etc
    version = ""                    # type: str # Effective version
    channel = ""                    # type: str # Channel (stable, latest, ...) via which this version was determined
    source = ""                     # type: str # Description of where definition came from

    # Runtime info, should be set/stored for 'current'
    packager = ""                   # type: str # Packager used
    delivery = ""                   # type: str # Delivery method used
    python = ""                     # type: str # Python interpreter used

    # Additional info
    pickley = ""                    # type: str # Pickley version used to perform install
    timestamp = None                # type: int # Epoch when version was determined (useful to cache "expensive" calls to pypi)

    def __init__(self, name, suffix=None):
        """
        :param str name: Associated pypi package name
        :param str|None suffix: Optional suffix where to store this object
        """
        self._name = name
        self._suffix = suffix
        if suffix:
            self._path = system.SETTINGS.meta.full_path(name, ".%s.json" % suffix)

    def __repr__(self):
        return self.representation()

    def _update_dynamic_fields(self):
        """Update dynamically determined fields"""
        if self._suffix != system.LATEST_CHANNEL:
            self.packager = PACKAGERS.resolved_name(self._name)
            self.delivery = DELIVERERS.resolved_name(self._name)
            python = system.target_python(package_name=self._name)
            self.python = python.text  # Record which python was used, as specified
        self.pickley = __version__
        self.timestamp = int(time.time())

    def representation(self, verbose=False, note=None):
        """
        :param bool verbose: If True, show more extensive info
        :param str|None note: Optional not to mention in returned text
        :return str: Human readable representation
        """
        if self._problem:
            lead = "%s: %s" % (self._name, self._problem)
        elif self.version:
            lead = "%s %s" % (self._name, self.version)
        else:
            lead = "%s: no version" % self._name
        notice = ""
        if verbose:
            notice = []
            if not self._problem and self.version and (self.packager or self.delivery):
                info = "as"
                if self.packager:
                    info = "%s %s" % (info, self.packager)
                if self.delivery:
                    info = "%s %s" % (info, self.delivery)
                notice.append(info)
            if self.channel:
                notice.append("channel: %s" % self.channel)
            if notice and self.source and self.source != system.SETTINGS.index:
                notice.append("source: %s" % self.source)
            if notice:
                notice = " (%s)" % ", ".join(notice)
            else:
                notice = ""
        if note:
            notice = " %s%s" % (note, notice)
        return "%s%s" % (lead, notice)

    @property
    def problem(self):
        """
        :return str|None: Problem description, if any
        """
        return self._problem

    @property
    def valid(self):
        """
        :return bool: Was version determined successfully?
        """
        return bool(self.version) and not self._problem

    @property
    def file_exists(self):
        """
        :return bool: True if corresponding json file exists
        """
        return self._path and os.path.exists(self._path)

    def equivalent(self, other):
        """
        :param VersionMeta|None other: VersionMeta to compare to
        :return bool: True if 'self' is equivalent to 'other'
        """
        if other is None:
            return False
        if self.version != other.version:
            return False
        if self.packager != other.packager:
            return False
        if self.delivery != other.delivery:
            return False
        return True

    def set_version(self, version, channel, source):
        """
        :param str version: Effective version
        :param str channel: Channel (stable, latest, ...) via which this version was determined
        :param str source: Description of where version determination came from
        """
        self.version = version
        self.channel = channel
        self.source = source
        self._update_dynamic_fields()

    def set_from(self, other):
        """
        :param VersionMeta other: Set this meta from 'other'
        """
        if isinstance(other, VersionMeta):
            self._problem = other._problem
            self.version = other.version
            self.channel = other.channel
            self.source = other.source
            self._update_dynamic_fields()

    def invalidate(self, problem):
        """
        :param str problem: Description of problem
        """
        self._problem = problem
        self.version = ""

    @property
    def still_valid(self):
        """
        :return bool: Is this version determination still valid? (based on timestamp)
        """
        if not self.valid or not self.timestamp:
            return self.valid
        try:
            return (int(time.time()) - self.timestamp) < system.SETTINGS.version_check_seconds
        except (TypeError, ValueError):
            return False


class Packager(object):
    """
    Interface of a packager
    """

    registered_name = None  # type: str # Injected by ImplementationMap
    spec = None  # type: str # Optional, version of underlying implementation to use (example: ==1.4.5)

    def __init__(self, name):
        """
        :param str name: Name of pypi package
        """
        self.name, self.version = system.despecced(name)
        self._entry_points = None
        self.current = VersionMeta(self.name, "current")
        self.latest = VersionMeta(self.name, system.LATEST_CHANNEL)
        self.desired = VersionMeta(self.name)
        self.dist_folder = system.SETTINGS.meta.full_path(self.name, ".tmp")
        self.build_folder = os.path.join(self.dist_folder, "build")
        self.relocatable = False
        self.sanity_check = None
        self.source_folder = None

    def __repr__(self):
        return "%s %s" % (self.registered_name, self.name)

    @property
    def specced_name(self):
        """
        :return str: Name of underlying pypi package to use, optionally with pinned version
        """
        if self.spec:
            return "%s==%s" % (self.registered_name, self.spec)
        return self.registered_name

    @property
    def entry_points_path(self):
        return system.SETTINGS.meta.full_path(self.name, ".entry-points.json")

    @property
    def removed_entry_points_path(self):
        return system.SETTINGS.meta.full_path(self.name, ".removed-entry-points.json")

    @property
    def entry_points(self):
        """
        :return dict: Determined entry points from produced wheel, if available
        """
        if self._entry_points is None:
            self._entry_points = JsonSerializable.get_json(self.entry_points_path)
            if isinstance(self._entry_points, list):
                # For backwards compatibility with pickley <= v1.4.2
                self._entry_points = dict((k, "") for k in self._entry_points)
            if self._entry_points is None:
                return {self.name: ""} if runez.DRYRUN else {}
        return self._entry_points

    @property
    def venv_python(self):
        """
        :return str: Python to use for relocatable venv
        """
        if system.DESIRED_PYTHON and os.path.isabs(system.DESIRED_PYTHON):
            return system.DESIRED_PYTHON
        if system.is_universal(self.build_folder):
            return "python"
        python = system.target_python(package_name=self.name)
        if os.path.isabs(python.text):
            return python.executable
        return python.program_name

    def refresh_entry_points(self, version):
        """
        :param str version: Version of package
        """
        if runez.DRYRUN:
            return
        self._entry_points = self.get_entry_points(version)
        JsonSerializable.save_json(self._entry_points, self.entry_points_path)

    def get_entry_points(self, version):
        """
        :param str version: Version of package
        :return dict|None: Determined entry points for pypi package with 'self.name'
        """
        if not os.path.isdir(self.build_folder):
            return None

        prefix = "%s-%s-" % (self.name, version)
        for fname in os.listdir(self.build_folder):
            if fname.startswith(prefix) and fname.endswith(".whl"):
                wheel_path = os.path.join(self.build_folder, fname)
                try:
                    with zipfile.ZipFile(wheel_path, "r") as wheel:
                        for wname in wheel.namelist():
                            if os.path.basename(wname) == "entry_points.txt":
                                with wheel.open(wname) as fh:
                                    return read_entry_points(fh)

                except Exception as e:
                    runez.error("Can't read wheel %s: %s", wheel_path, e, exc_info=e)

        return None

    def refresh_current(self):
        """Refresh self.current"""
        self.current.load()
        if not self.current.valid:
            self.current.invalidate("is not installed")

    def refresh_latest(self, force=False):
        """Refresh self.latest"""
        self.latest.load()
        if not force and self.latest.still_valid:
            return

        version = latest_pypi_version(system.SETTINGS.index, self.name)
        source = system.SETTINGS.index or "pypi"
        self.latest.set_version(version, system.LATEST_CHANNEL, source)
        if version and not version.startswith("can't"):
            self.latest.save()

        else:
            self.latest.invalidate(version or "can't determine latest version from %s" % source)

    def refresh_desired(self, force=False):
        """Refresh self.desired"""
        channel = system.SETTINGS.resolved_definition("channel", package_name=self.name)
        v = system.SETTINGS.get_definition("channel.%s.%s" % (channel.value, self.name))
        if v and v.value:
            self.desired.set_version(v.value, channel.value, str(v))
            return

        if channel.value == system.LATEST_CHANNEL:
            self.refresh_latest(force=force)
            self.desired.set_from(self.latest)
            return

        self.desired.invalidate("can't determine %s version" % channel.value)

    def pip_wheel(self, version):
        """
        Run pip wheel

        :param str version: Version to use
        :return str: None if successful, error message otherwise
        """
        runez.ensure_folder(self.build_folder, folder=True)
        return vrun(
            self.name,
            "pip", "wheel",
            "-i", system.SETTINGS.index,
            "--cache-dir", self.build_folder,
            "--wheel-dir", self.build_folder,
            self.source_folder if self.source_folder else "%s==%s" % (self.name, version)
        )

    def package(self, version=None):
        """
        :param str|None version: If provided, append version as suffix to produced pex
        :return list: List of produced packages (files), if successful
        """
        if not version and not self.source_folder:
            return runez.abort("Need either source_folder or version in order to package", return_value=[])

        if not version:
            setup_py = os.path.join(self.source_folder, "setup.py")
            if not os.path.isfile(setup_py):
                return runez.abort("No setup.py in %s", short(self.source_folder), return_value=[])
            version = system.run_python(setup_py, "--version", fatal=False, package_name=self.name)
            if not version:
                return runez.abort("Could not determine version from %s", short(setup_py), return_value=[])

        self.pip_wheel(version)

        self.refresh_entry_points(version)
        runez.ensure_folder(self.dist_folder, folder=True)
        template = "{name}" if self.source_folder else "{name}-{version}"
        r = self.effective_package(template, version)
        if self.sanity_check:
            pass
        return r

    def effective_package(self, template, version=None):
        """
        :param str template: Template describing how to name delivered files, example: {meta}/{name}-{version}
        :param str|None version: If provided, append version as suffix to produced pex
        :return list: List of produced packages (files), if successful
        """
        return []

    def install(self, force=False):
        """
        :param bool force: If True, re-install even if package is already installed
        """
        try:
            self.internal_install(force=force)

        except SoftLockException as e:
            runez.error("%s is currently being installed by another process" % self.name)
            runez.abort("If that is incorrect, please delete %s.lock", short(e.folder))

    def internal_install(self, force=False, verbose=True):
        """
        :param bool force: If True, re-install even if package is already installed
        :param bool verbose: If True, show more extensive info
        """
        with SoftLock(self.dist_folder, timeout=system.SETTINGS.install_timeout):
            self.refresh_desired(force=force)
            if not self.desired.valid:
                return runez.abort("Can't install %s: %s", self.name, self.desired.problem)

            self.refresh_current()
            self.desired.delivery = DELIVERERS.resolved_name(self.name, default=self.current.delivery)
            if not force and self.current.equivalent(self.desired):
                runez.info(self.desired.representation(verbose=verbose, note="is already installed"))
                self.cleanup()
                return

            system.setup_audit_log()

            prev_entry_points = self.entry_points
            self.effective_install(self.desired.version)

            new_entry_points = self.entry_points
            removed = set(prev_entry_points).difference(new_entry_points)
            if removed:
                old_removed = JsonSerializable.get_json(self.removed_entry_points_path, default=[])
                removed = sorted(removed.union(old_removed))
                JsonSerializable.save_json(removed, self.removed_entry_points_path)

            # Delete wrapper/symlinks of removed entry points immediately
            for name in removed:
                runez.delete(system.SETTINGS.base.full_path(name))

            self.cleanup()

            self.current.set_from(self.desired)
            self.current.save()

            msg = "Would install" if runez.DRYRUN else "Installed"
            runez.info("%s %s", msg, self.desired.representation(verbose=verbose))

    def cleanup(self):
        """Cleanup older installs"""
        cutoff = time.time() - system.SETTINGS.install_timeout * 60
        folder = system.SETTINGS.meta.full_path(self.name)

        removed_entry_points = JsonSerializable.get_json(self.removed_entry_points_path, default=[])

        prefixes = {None: [], self.name: []}
        for name in self.entry_points:
            prefixes[name] = []
        for name in removed_entry_points:
            prefixes[name] = []

        if os.path.isdir(folder):
            for name in os.listdir(folder):
                if name.startswith("."):
                    continue
                target = find_prefix(prefixes, name)
                if target in prefixes:
                    fpath = os.path.join(folder, name)
                    prefixes[target].append((os.path.getmtime(fpath), fpath))

        # Sort each by last modified timestamp
        for target, cleanable in prefixes.items():
            prefixes[target] = sorted(cleanable, reverse=True)

        rem_cleaned = 0
        for target, cleanable in prefixes.items():
            if not cleanable:
                if target in removed_entry_points:
                    # No cleanable found for a removed entry-point -> count as cleaned
                    rem_cleaned += 1
                continue

            if target not in removed_entry_points:
                if cleanable[0][0] <= cutoff:
                    # Latest is old enough now, cleanup all except latest
                    cleanable = cleanable[1:]
                else:
                    # Latest is too young, keep the last 2
                    cleanable = cleanable[2:]
            elif cleanable[0][0] <= cutoff:
                # Delete all removed entry points when old enough
                rem_cleaned += 1
            else:
                # Removed entry point still too young, keep latest
                cleanable = cleanable[1:]

            for _, path in cleanable:
                runez.delete(path)

        if rem_cleaned >= len(removed_entry_points):
            runez.delete(self.removed_entry_points_path)

    def effective_install(self, version):
        """
        :param str version: Effective version to install
        :return list: Full path to installed files/folders
        """

    def required_entry_points(self):
        """
        :return list: Entry points, abort execution if there aren't any
        """
        ep = self.entry_points
        if not ep:
            runez.delete(system.SETTINGS.meta.full_path(self.name))
            runez.abort("'%s' is not a CLI, it has no console_scripts entry points", self.name)
        return ep

    def perform_delivery(self, version, template):
        """
        :param str version: Version being delivered
        :param str template: Template describing how to name delivered files, example: {meta}/{name}-{version}
        """
        # Touch the .ping file since this is a fresh install (no need to check for upgrades right away)
        runez.touch(system.SETTINGS.meta.full_path(self.name, ".ping"))

        deliverer = DELIVERERS.resolved(self.name, default=self.desired.delivery)
        for name in self.required_entry_points():
            target = system.SETTINGS.base.full_path(name)
            if self.name != system.PICKLEY and not self.current.file_exists:
                uninstall_existing(target)
            path = template.format(meta=system.SETTINGS.meta.full_path(self.name), name=name, version=version)
            deliverer.install(target, path)


@PACKAGERS.register
class PexPackager(Packager):
    """
    Package/install via pex (https://pypi.org/project/pex/)
    """

    def pex_build(self, name, version, destination):
        """
        Run pex build

        :param str name: Name of entry point
        :param str version: Version to use
        :param str destination: Path to file where to produce pex
        :return str: None if successful, error message otherwise
        """
        runez.ensure_folder(self.build_folder, folder=True)
        runez.delete(destination)

        args = ["--cache-dir", self.build_folder, "--repo", self.build_folder]
        args.extend(["-c%s" % name, "-o%s" % destination, "%s==%s" % (self.name, version)])

        python = system.target_python(package_name=self.name)
        shebang = python.shebang(universal=system.is_universal(self.build_folder))
        if shebang:
            args.append("--python-shebang")
            args.append(shebang)

        vrun(self.name, self.specced_name, *args, path_env=C_COMPILATION_HELP)

    def effective_package(self, template, version=None):
        """
        :param str template: Template describing how to name delivered files, example: {meta}/{name}-{version}
        :param str|None version: If provided, append version as suffix to produced pex
        :return list: List of produced packages (files), if successful
        """
        result = []
        for name in self.required_entry_points():
            dest = template.format(name=name, version=version)
            dest = os.path.join(self.dist_folder, dest)

            self.pex_build(name, version, dest)
            result.append(dest)

        return result

    def effective_install(self, version):
        """
        :param str version: Effective version to install
        :return list: Full path to installed files/folders
        """
        result = []
        packaged = self.package(version=version)
        for path in packaged:
            name = os.path.basename(path)
            target = system.SETTINGS.meta.full_path(self.name, name)
            system.move(path, target)
            result.append(target)
        self.perform_delivery(version, "{meta}/{name}-{version}")
        return result


@PACKAGERS.register
class VenvPackager(Packager):
    """
    Install via virtualenv (https://pypi.org/project/virtualenv/)
    """

    def effective_package(self, template, version=None):
        """
        :param str template: Template describing how to name delivered files, example: {meta}/{name}-{version}
        :param str|None version: If provided, append version as suffix to produced pex
        :return list: List of produced packages (files), if successful
        """
        folder = os.path.join(self.dist_folder, template.format(name=self.name, version=version))
        runez.ensure_folder(folder, folder=True)
        vrun(self.name, "virtualenv", folder)

        pip = os.path.join(folder, "bin", "pip")
        runez.run_program(pip, "install", "-i", system.SETTINGS.index, "-f", self.build_folder, "%s==%s" % (self.name, version))

        if self.relocatable:
            vrun(self.name, "virtualenv", "-p", self.venv_python, "--relocatable", os.path.join(self.dist_folder, self.name))

        return [folder]

    def effective_install(self, version):
        """
        :param str version: Effective version to install
        :return list: Full path to installed files/folders
        """
        result = []
        packaged = self.package(version=version)
        for path in packaged:
            target = system.SETTINGS.meta.full_path(self.name, os.path.basename(path))
            system.move(path, target)
            result.append(target)
            self.perform_delivery(version, os.path.join(target, "bin", "{name}"))
        return result
