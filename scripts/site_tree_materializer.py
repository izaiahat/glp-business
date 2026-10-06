"""Space-safe site-tree materialization for immutable release bases.

A candidate starts as hardlinks to an immutable predecessor. Before any
candidate path is written, callers must call :func:`detach_for_write` for that
path. Directories are always private. Files stay shared until detached.

The helper deliberately has no cross-device hardlink-copy fallback. Privileged
operations use already-open descriptors within fixed flagship site roots. The
narrow authenticated registry prepared-base producer creates private immutable
bytes there; candidate/release construction still uses hardlink COW.
"""
from __future__ import annotations

import errno
import hashlib
import os
import resource
import secrets
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


_STAT_FIELDS = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_mtime_ns", "st_size")
_PRESERVED_FIELDS = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_mtime_ns", "st_size")
_BUSINESS_ROOT = Path("/home/ubuntu/business")
_SITE_ROOT = _BUSINESS_ROOT / "site-system/flagship-v2"
_PRIVILEGED_SOURCE_PARENTS = (_SITE_ROOT / "releases", _SITE_ROOT / "candidates")
_PRIVILEGED_DESTINATION_PARENTS = (_SITE_ROOT / "releases", _SITE_ROOT / "candidates")
_CANONICAL_MACHINE_ROOT = _BUSINESS_ROOT / "reports/glp-clinic-scout/affiliate-machine"
_PROTECTED_CANONICAL_TARGETS = (
    _CANONICAL_MACHINE_ROOT / "applications-ledger.json",
    _CANONICAL_MACHINE_ROOT / "operator-blockers.json",
    _CANONICAL_MACHINE_ROOT / "daily-report-state.json",
)
FLAGSHIP_REQUIRED_PUBLIC_BUILD_INPUTS: tuple[Path, ...] = (
    # Exact predecessor-owned inputs read by build-staging.mjs before it resets
    # public/. Keep this list synchronized with those pre-reset reads. The weekly
    # price-index chain hit ENOENT on _headers on 2026-08-21 because this set was
    # empty; google-ads-api would have been the next missing input.
    Path("public/_headers"),
    Path("public/google-ads-api/404.html"),
    Path("public/google-ads-api/index.html"),
    Path("public/google-ads-api/privacy/index.html"),
    Path("public/google-ads-api/support/index.html"),
    Path("public/google-ads-api/terms/index.html"),
    Path("public/google-ads-api/assets/site.css"),
    Path("public/google-ads-api/assets/favicon.svg"),
)
_SUDO = "/usr/bin/sudo"
_CP = "/usr/bin/cp"
_CHOWN = "/usr/bin/chown"
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_MAX_TREE_DIRECTORY_FDS = 65_536
_DIRECTORY_FD_SAFETY_MARGIN = 64
CONSTRUCTOR_PROVENANCE_SCHEMA = "site-tree-constructor-provenance-v1"
HARDLINK_COW_POLICY = {
    "candidate_or_release_full_copy": "forbidden",
    "materialization_api": "site_tree_materializer.copytree_linked",
    "mutation_paths": "detached_before_write",
    "unchanged_paths": "share_predecessor_inode",
    "unchanged_paths_single_link_requirement": "forbidden",
}

IgnoreCallable = Callable[[str, list[str]], Iterable[str]]


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _snapshot(info: os.stat_result) -> tuple[int, ...]:
    return tuple(int(getattr(info, field)) for field in _STAT_FIELDS)


def _preserved_snapshot(info: os.stat_result) -> tuple[int, ...]:
    return tuple(int(getattr(info, field)) for field in _PRESERVED_FIELDS)


def _normalized_absolute(value: Path | str, label: str) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw.startswith("/") or raw != os.path.normpath(raw):
        raise ValueError(f"{label}_absolute_normalized_path_required:{raw}")
    path = Path(raw)
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise ValueError(f"{label}_path_traversal_forbidden:{raw}")
    return path


def _contains_path(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _assert_materialization_scope(source: Path, destination: Path) -> None:
    """Reject recursive trees and sources that contain canonical mutable state.

    Paths are normalized absolute values here. Symlink aliases are rejected by
    the descriptor-bound open that follows, so no alternate spelling can turn a
    protected business-tree source into a valid site-tree source.
    """
    protected = [
        target for target in _PROTECTED_CANONICAL_TARGETS
        if _contains_path(source, target)
    ]
    if protected:
        raise PermissionError(
            f"linked_source_contains_protected_canonical_state:{source}"
        )
    if _contains_path(source, destination):
        raise ValueError(f"linked_destination_inside_source:{destination}")


def _open_directory_nofollow(path: Path) -> int:
    """Open an absolute directory one component at a time without symlinks."""
    path = _normalized_absolute(path, "directory")
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_tree_root(path: Path) -> int:
    try:
        return _open_directory_nofollow(path)
    except OSError as exc:
        raise ValueError(f"tree_symlink_ancestor_or_directory_invalid:{path}:{exc.errno}") from None


def _fixed_child_parent(path: Path, allowed: Sequence[Path], label: str) -> Path:
    if path.name in {"", ".", ".."} or path.name.startswith("-"):
        raise ValueError(f"{label}_child_name_invalid:{path.name}")
    if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in path.name):
        raise ValueError(f"{label}_child_name_invalid:{path.name}")
    matches = [parent for parent in allowed if path.parent == parent]
    if len(matches) != 1:
        raise PermissionError(f"{label}_outside_fixed_site_roots:{path}")
    return matches[0]


def _entry_stat(directory_fd: int, name: str) -> os.stat_result:
    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"tree_symlink_forbidden:{name}")
    return info


def _opened_stat(directory_fd: int, name: str, flags: int, expected: os.stat_result) -> tuple[int, os.stat_result]:
    fd = os.open(name, flags, dir_fd=directory_fd)
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino, opened.st_mode) != (expected.st_dev, expected.st_ino, expected.st_mode):
        os.close(fd)
        raise RuntimeError(f"tree_entry_raced:{name}")
    return fd, opened


def _ignore_map_fd(
    root_fd: int,
    root: Path,
    ignore: IgnoreCallable | None,
) -> dict[str, frozenset[str]]:
    """Evaluate shutil-compatible ignore rules once against descriptor-bound names."""
    if ignore is None:
        return {}
    ignored: dict[str, frozenset[str]] = {}

    def walk(directory_fd: int, relative: Path) -> None:
        names = sorted(os.listdir(directory_fd))
        raw = ignore(os.fspath(root / relative), list(names))
        try:
            selected = frozenset(raw)
        except TypeError as exc:
            raise ValueError("linked_ignore_result_invalid") from exc
        if any(
            not isinstance(name, str)
            or name not in names
            or name in {"", ".", ".."}
            or "/" in name
            or "\\" in name
            for name in selected
        ):
            raise ValueError(f"linked_ignore_result_invalid:{relative}")
        key = relative.as_posix() if relative.parts else "."
        if selected:
            ignored[key] = selected
        for name in names:
            if name in selected:
                continue
            info = _entry_stat(directory_fd, name)
            if stat.S_ISDIR(info.st_mode):
                child_fd, _opened = _opened_stat(directory_fd, name, _DIRECTORY_FLAGS, info)
                try:
                    walk(child_fd, relative / name)
                finally:
                    os.close(child_fd)
            elif not stat.S_ISREG(info.st_mode):
                raise ValueError(f"tree_regular_file_or_directory_required:{relative / name}")

    walk(root_fd, Path())
    return ignored


def _ignored_names(ignore_map: dict[str, frozenset[str]], prefix: Path) -> frozenset[str]:
    key = prefix.as_posix() if prefix.parts else "."
    return ignore_map.get(key, frozenset())


def _tree_manifest_fd(
    root_fd: int,
    *,
    require_owner: tuple[int, int] | None = None,
    require_immutable: bool = True,
    ignore_map: dict[str, frozenset[str]] | None = None,
) -> dict[str, dict[str, tuple[int, ...]]]:
    directories: dict[str, tuple[int, ...]] = {}
    files: dict[str, tuple[int, ...]] = {}

    def validate(info: os.stat_result, relative: str, expected_kind: str) -> None:
        if expected_kind == "directory" and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"tree_directory_required:{relative}")
        if expected_kind == "file" and not stat.S_ISREG(info.st_mode):
            raise ValueError(f"tree_regular_file_required:{relative}")
        if require_immutable and stat.S_IMODE(info.st_mode) & 0o222:
            raise ValueError(f"immutable_base_writable:{relative}")
        if require_owner is not None and (info.st_uid, info.st_gid) != require_owner:
            raise PermissionError(
                f"privileged_base_owner_invalid:{relative}:{info.st_uid}:{info.st_gid}"
            )

    root_info = os.fstat(root_fd)
    validate(root_info, ".", "directory")
    directories["."] = _preserved_snapshot(root_info)

    def walk(directory_fd: int, prefix: Path) -> None:
        ignored = _ignored_names(ignore_map or {}, prefix)
        for name in sorted(os.listdir(directory_fd)):
            if name in {".", ".."}:
                raise RuntimeError("tree_reserved_entry_name")
            if name in ignored:
                continue
            relative_path = prefix / name
            relative = relative_path.as_posix()
            info = _entry_stat(directory_fd, name)
            if stat.S_ISDIR(info.st_mode):
                child_fd, opened = _opened_stat(directory_fd, name, _DIRECTORY_FLAGS, info)
                try:
                    validate(opened, relative, "directory")
                    directories[relative] = _preserved_snapshot(opened)
                    walk(child_fd, relative_path)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(info.st_mode):
                file_fd, opened = _opened_stat(directory_fd, name, _FILE_FLAGS, info)
                try:
                    validate(opened, relative, "file")
                    files[relative] = _preserved_snapshot(opened)
                finally:
                    os.close(file_fd)
            else:
                raise ValueError(f"tree_regular_file_or_directory_required:{relative}")

    walk(root_fd, Path())
    return {"directories": directories, "files": files}


def _manifest_owners(manifest: dict[str, dict[str, tuple[int, ...]]]) -> set[tuple[int, int]]:
    # uid/gid occupy indexes 3 and 4 in _PRESERVED_FIELDS.
    return {(value[3], value[4]) for group in manifest.values() for value in group.values()}


def _proc_fd_path(fd: int, *, contents: bool = False) -> str:
    suffix = "/." if contents else ""
    return f"/proc/{os.getpid()}/fd/{fd}{suffix}"


def _sudo_copy_between_fds(source_fd: int, destination_fd: int) -> None:
    """Run only the fixed cp operation against already-validated open FDs."""
    proc = subprocess.run(
        [
            _SUDO,
            "-n",
            "--",
            _CP,
            "--archive",
            "--link",
            "--",
            _proc_fd_path(source_fd, contents=True),
            _proc_fd_path(destination_fd, contents=True),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"privileged_hardlink_copy_failed:{proc.stderr.strip()[-500:]}")


def _sudo_chown_fds(fds: Sequence[int], uid: int, gid: int) -> int:
    if not fds:
        return 0
    changed = 0
    for start in range(0, len(fds), 100):
        chunk = fds[start:start + 100]
        proc = subprocess.run(
            [
                _SUDO,
                "-n",
                "--",
                _CHOWN,
                f"{uid}:{gid}",
                "--",
                *(_proc_fd_path(fd) for fd in chunk),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"privileged_descriptor_chown_failed:{proc.stderr.strip()[-500:]}")
        changed += len(chunk)
    return changed


def _count_directories_fd(root_fd: int) -> int:
    """Count directories with depth-bounded descriptors before all-FD handoff."""
    count = 1

    def walk(directory_fd: int) -> None:
        nonlocal count
        for name in sorted(os.listdir(directory_fd)):
            info = _entry_stat(directory_fd, name)
            if stat.S_ISDIR(info.st_mode):
                child_fd, _opened = _opened_stat(directory_fd, name, _DIRECTORY_FLAGS, info)
                try:
                    count += 1
                    walk(child_fd)
                finally:
                    os.close(child_fd)
            elif not stat.S_ISREG(info.st_mode):
                raise ValueError(f"tree_regular_file_or_directory_required:{name}")

    walk(root_fd)
    return count


def _raise_bounded_fd_limit(required_fds: int, label: str) -> tuple[int, int]:
    previous_soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    open_now = len(os.listdir("/proc/self/fd"))
    required_soft = open_now + required_fds + _DIRECTORY_FD_SAFETY_MARGIN
    hard_ceiling = (
        _MAX_TREE_DIRECTORY_FDS
        if hard == resource.RLIM_INFINITY
        else min(int(hard), _MAX_TREE_DIRECTORY_FDS)
    )
    if required_soft > hard_ceiling:
        raise OSError(
            errno.EMFILE,
            f"{label}_descriptor_budget_exceeded:{required_fds}:{hard_ceiling}",
        )
    if required_soft > previous_soft:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (required_soft, hard))
        except (OSError, ValueError) as exc:
            raise OSError(
                errno.EMFILE,
                f"{label}_descriptor_limit_unavailable:{required_soft}",
            ) from exc
    return int(previous_soft), int(hard)


def _collect_directory_fds(root_fd: int) -> list[int]:
    collected = [os.dup(root_fd)]

    def walk(directory_fd: int) -> None:
        for name in sorted(os.listdir(directory_fd)):
            info = _entry_stat(directory_fd, name)
            if stat.S_ISDIR(info.st_mode):
                child_fd, _opened = _opened_stat(directory_fd, name, _DIRECTORY_FLAGS, info)
                collected.append(child_fd)
                walk(child_fd)
            elif not stat.S_ISREG(info.st_mode):
                raise ValueError(f"tree_regular_file_or_directory_required:{name}")

    try:
        walk(root_fd)
        return collected
    except Exception:
        for fd in collected:
            os.close(fd)
        raise


def _handoff_directories(root_fd: int, uid: int, gid: int, *, privileged: bool) -> int:
    directory_count = _count_directories_fd(root_fd)
    previous_limit = _raise_bounded_fd_limit(directory_count, "tree_directory")
    fds: list[int] = []
    try:
        fds = _collect_directory_fds(root_fd)
        if len(fds) != directory_count:
            raise RuntimeError("directory_handoff_count_changed")
        pending = [fd for fd in fds if (os.fstat(fd).st_uid, os.fstat(fd).st_gid) != (uid, gid)]
        if pending:
            if os.geteuid() == 0:
                for fd in pending:
                    os.fchown(fd, uid, gid)
            elif privileged:
                _sudo_chown_fds(pending, uid, gid)
            else:
                raise PermissionError("directory_handoff_requires_privilege")
        for fd in fds:
            info = os.fstat(fd)
            os.fchmod(fd, stat.S_IMODE(info.st_mode) | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            final = os.fstat(fd)
            if (final.st_uid, final.st_gid) != (uid, gid) or not stat.S_IMODE(final.st_mode) & stat.S_IWUSR:
                raise RuntimeError("directory_handoff_verification_failed")
        return len(fds)
    finally:
        for fd in fds:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, previous_limit)


def _destination_binding_matches(parent_fd: int, name: str, destination_fd: int) -> bool:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    opened = os.fstat(destination_fd)
    return stat.S_ISDIR(named.st_mode) and (named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino)


def _clear_directory_fd(root_fd: int) -> None:
    info = os.fstat(root_fd)
    os.fchmod(root_fd, stat.S_IMODE(info.st_mode) | 0o700)
    for name in sorted(os.listdir(root_fd)):
        entry = _entry_stat(root_fd, name)
        if stat.S_ISDIR(entry.st_mode):
            child_fd, _opened = _opened_stat(root_fd, name, _DIRECTORY_FLAGS, entry)
            try:
                _clear_directory_fd(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=root_fd)
        else:
            os.unlink(name, dir_fd=root_fd)


def _open_relative_directory_fd(root_fd: int, relative: str) -> int:
    fd = os.dup(root_fd)
    if relative == ".":
        return fd
    try:
        for component in Path(relative).parts:
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _remove_entry_fd(parent_fd: int, name: str) -> None:
    info = _entry_stat(parent_fd, name)
    if stat.S_ISDIR(info.st_mode):
        child_fd, _opened = _opened_stat(parent_fd, name, _DIRECTORY_FLAGS, info)
        try:
            _clear_directory_fd(child_fd)
        finally:
            os.close(child_fd)
        os.rmdir(name, dir_fd=parent_fd)
    elif stat.S_ISREG(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
    else:
        raise ValueError(f"tree_regular_file_or_directory_required:{name}")


def _remove_ignored_destination_entries(
    destination_fd: int,
    ignore_map: dict[str, frozenset[str]],
) -> None:
    for relative in sorted(ignore_map, key=lambda item: len(Path(item).parts), reverse=True):
        try:
            parent_fd = _open_relative_directory_fd(destination_fd, relative)
        except FileNotFoundError:
            continue
        try:
            for name in sorted(ignore_map[relative]):
                try:
                    _remove_entry_fd(parent_fd, name)
                except FileNotFoundError:
                    continue
        finally:
            os.close(parent_fd)


def _cleanup_destination(parent_fd: int, name: str, destination_fd: int, *, privileged: bool) -> None:
    """Remove a failed materialization or raise an unmistakable cleanup error.

    Cleanup is best-effort only while the destination name is ultimately absent.
    A handoff failure must never make this function silently return with the
    partially linked directory still addressable at the requested destination.
    """
    errors: list[str] = []
    try:
        _handoff_directories(destination_fd, os.geteuid(), os.getegid(), privileged=privileged)
    except Exception as exc:
        errors.append(f"handoff:{type(exc).__name__}:{exc}")
    try:
        _clear_directory_fd(destination_fd)
    except Exception as exc:
        errors.append(f"clear:{type(exc).__name__}:{exc}")
    if _destination_binding_matches(parent_fd, name, destination_fd):
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError as exc:
            errors.append(f"rmdir:{exc.errno}:{exc}")
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        errors.append(f"readback:{exc.errno}:{exc}")
    else:
        errors.append("destination_still_present")
    detail = "|".join(errors) if errors else "destination_still_present"
    raise RuntimeError(f"linked_destination_cleanup_failed:{detail}")


def _link_tree_direct(
    source_fd: int,
    destination_fd: int,
    ignore_map: dict[str, frozenset[str]] | None = None,
) -> tuple[int, int, int]:
    linked_files = 0
    linked_bytes = 0
    directories = 1

    def walk(source_directory_fd: int, destination_directory_fd: int, prefix: Path) -> None:
        nonlocal linked_files, linked_bytes, directories
        ignored = _ignored_names(ignore_map or {}, prefix)
        for name in sorted(os.listdir(source_directory_fd)):
            if name in ignored:
                continue
            source_info = _entry_stat(source_directory_fd, name)
            if stat.S_ISDIR(source_info.st_mode):
                source_child_fd, _opened = _opened_stat(
                    source_directory_fd, name, _DIRECTORY_FLAGS, source_info
                )
                try:
                    os.mkdir(name, 0o700, dir_fd=destination_directory_fd)
                    destination_child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=destination_directory_fd)
                    try:
                        os.fchmod(
                            destination_child_fd,
                            stat.S_IMODE(source_info.st_mode) | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
                        )
                        directories += 1
                        walk(source_child_fd, destination_child_fd, prefix / name)
                    finally:
                        os.close(destination_child_fd)
                finally:
                    os.close(source_child_fd)
            elif stat.S_ISREG(source_info.st_mode):
                source_file_fd, opened = _opened_stat(
                    source_directory_fd, name, _FILE_FLAGS, source_info
                )
                try:
                    os.link(
                        name,
                        name,
                        src_dir_fd=source_directory_fd,
                        dst_dir_fd=destination_directory_fd,
                        follow_symlinks=False,
                    )
                    destination_info = os.stat(
                        name, dir_fd=destination_directory_fd, follow_symlinks=False
                    )
                    if (destination_info.st_dev, destination_info.st_ino) != (
                        opened.st_dev,
                        opened.st_ino,
                    ):
                        raise RuntimeError(f"linked_inode_mismatch:{name}")
                    linked_files += 1
                    linked_bytes += opened.st_size
                finally:
                    os.close(source_file_fd)
            else:
                raise ValueError(f"tree_regular_file_or_directory_required:{name}")

    walk(source_fd, destination_fd, Path())
    return linked_files, linked_bytes, directories


def _verify_linked_files(
    source_manifest: dict[str, dict[str, tuple[int, ...]]], destination_fd: int
) -> tuple[int, int]:
    destination_manifest = _tree_manifest_fd(destination_fd, require_immutable=False)
    if set(destination_manifest["files"]) != set(source_manifest["files"]):
        raise RuntimeError("linked_materialization_file_set_mismatch")
    if set(destination_manifest["directories"]) != set(source_manifest["directories"]):
        raise RuntimeError("linked_materialization_directory_set_mismatch")
    linked_bytes = 0
    for relative, source_info in source_manifest["files"].items():
        destination_info = destination_manifest["files"][relative]
        if destination_info[:2] != source_info[:2]:
            raise RuntimeError(f"linked_inode_mismatch:{relative}")
        if destination_info[2:] != source_info[2:]:
            raise RuntimeError(f"linked_metadata_mismatch:{relative}")
        linked_bytes += source_info[-1]
    return len(source_manifest["files"]), linked_bytes


def _regular_files_and_directories(root: Path) -> tuple[list[Path], list[Path]]:
    """Return a symlink-free regular-file tree, rejecting special entries."""
    root = _normalized_absolute(root, "tree_root")
    root_fd = _open_tree_root(root)
    os.close(root_fd)
    if not root.is_dir():
        raise ValueError(f"tree_root_directory_required:{root}")
    directories = [root]
    files: list[Path] = []
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        current_info = os.lstat(current_path)
        if not stat.S_ISDIR(current_info.st_mode):
            raise ValueError(f"tree_directory_required:{current_path}")
        for name in sorted(dirnames):
            path = current_path / name
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"tree_symlink_forbidden:{path}")
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"tree_directory_required:{path}")
            directories.append(path)
        for name in sorted(filenames):
            path = current_path / name
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"tree_symlink_forbidden:{path}")
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"tree_regular_file_required:{path}")
            files.append(path)
    return directories, files


def _validate_immutable_base(root: Path) -> tuple[list[Path], list[Path]]:
    directories, files = _regular_files_and_directories(root)
    for path in [*directories, *files]:
        if stat.S_IMODE(os.lstat(path).st_mode) & 0o222:
            raise ValueError(f"immutable_base_writable:{path}")
    return directories, files


def make_directories_writable(root: Path) -> int:
    """Add owner rwx to private directories without touching any file inode."""
    directories, _files = _regular_files_and_directories(root)
    for path in sorted(directories, key=lambda item: len(item.parts)):
        mode = stat.S_IMODE(os.lstat(path).st_mode)
        os.chmod(path, mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR, follow_symlinks=False)
    return len(directories)


def remove_materialized_tree(destination: Path) -> None:
    """Remove one private candidate tree without changing any linked file inode."""
    destination = _normalized_absolute(destination, "remove_materialized_tree")
    if not _lexists(destination):
        return
    if destination.is_symlink() or not destination.is_dir():
        destination.unlink()
        return
    for current, dirnames, _filenames in os.walk(destination, topdown=True, followlinks=False):
        path = Path(current)
        os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) | 0o700, follow_symlinks=False)
        for name in dirnames:
            child = path / name
            if not child.is_symlink():
                os.chmod(child, stat.S_IMODE(os.lstat(child).st_mode) | 0o700, follow_symlinks=False)
    shutil.rmtree(destination)


def prepare_clean_build_trees(
    root: Path,
    generated_trees: Sequence[Path],
    preserved_inputs: Sequence[Path] = FLAGSHIP_REQUIRED_PUBLIC_BUILD_INPUTS,
) -> dict[str, Any]:
    """Reset generated output while retaining only detached build inputs.

    ``build-staging.mjs`` reads a small exact set of predecessor-owned files
    before deleting ``public`` itself. This routine detaches those exact files,
    snapshots their bytes, removes every generated tree, and recreates only
    those inputs as private files. No stale generated output can reach npm and
    no predecessor inode can be written.
    """
    root = _normalized_absolute(root, "clean_build_root")

    def relative_paths(values: Sequence[Path], label: str) -> tuple[Path, ...]:
        normalized: list[Path] = []
        for raw in values:
            value = Path(raw)
            if value.is_absolute() or not value.parts or any(part in {"", ".", ".."} for part in value.parts):
                raise ValueError(f"{label}_relative_path_invalid:{value}")
            if value in normalized:
                raise ValueError(f"{label}_duplicate:{value}")
            normalized.append(value)
        return tuple(normalized)

    trees = relative_paths(generated_trees, "generated_tree")
    inputs = relative_paths(preserved_inputs, "preserved_input")
    for left in trees:
        for right in trees:
            if left != right and (left in right.parents or right in left.parents):
                raise ValueError(f"generated_tree_overlap:{left}:{right}")
    for relative in inputs:
        containing = [tree for tree in trees if tree in relative.parents]
        if len(containing) != 1:
            raise ValueError(f"preserved_input_outside_generated_tree:{relative}")

    # Validate every prerequisite before the first detach/write.
    for relative in inputs:
        path = root / relative
        if not _lexists(path) or path.is_symlink() or not path.is_file():
            raise RuntimeError(f"required_clean_build_input_missing:{path}")
        parent_fd = _open_tree_root(path.parent)
        os.close(parent_fd)
    for relative in trees:
        path = root / relative
        if path.is_symlink():
            raise RuntimeError(f"generated_tree_symlink_forbidden:{path}")
        if path.exists() and not path.is_dir():
            raise RuntimeError(f"generated_tree_directory_required:{path}")

    payloads: list[dict[str, Any]] = []
    for relative in inputs:
        path = root / relative
        detached = detach_for_write(path)
        info = os.lstat(path)
        payload = path.read_bytes()
        payloads.append(
            {
                "relative": relative,
                "bytes_value": payload,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
                "mode": stat.S_IMODE(info.st_mode),
                "atime_ns": info.st_atime_ns,
                "mtime_ns": info.st_mtime_ns,
                "base_inode": detached["source_inode"],
                "detached_inode": detached["detached_inode"],
            }
        )

    removed: list[str] = []
    for relative in trees:
        path = root / relative
        if not path.exists():
            continue
        shutil.rmtree(path)
        removed.append(relative.as_posix())

    reports: list[dict[str, Any]] = []
    for row in payloads:
        relative = row["relative"]
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, row["mode"] | stat.S_IWUSR)
        try:
            view = memoryview(row["bytes_value"])
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
            os.fchmod(fd, row["mode"] | stat.S_IWUSR)
        finally:
            os.close(fd)
        os.utime(path, ns=(row["atime_ns"], row["mtime_ns"]), follow_symlinks=False)
        clean = os.lstat(path)
        # The detached inode was unlinked with the generated tree and the kernel may
        # legitimately reuse that inode number for this new O_EXCL file. The live
        # predecessor inode cannot be reused while the predecessor still exists.
        if clean.st_ino == row["base_inode"]:
            raise RuntimeError(f"clean_build_input_not_private:{path}")
        if clean.st_nlink != 1 or not stat.S_IMODE(clean.st_mode) & stat.S_IWUSR:
            raise RuntimeError(f"clean_build_input_not_owner_private:{path}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise RuntimeError(f"clean_build_input_byte_mismatch:{path}")
        reports.append(
            {
                "path": relative.as_posix(),
                "sha256": row["sha256"],
                "bytes": row["bytes"],
                "base_inode": row["base_inode"],
                "detached_inode": row["detached_inode"],
                "clean_inode": clean.st_ino,
                "clean_nlink": clean.st_nlink,
                "detached_before_reset": True,
            }
        )

    actual_files: list[str] = []
    for relative in trees:
        path = root / relative
        if path.exists():
            for entry in sorted(path.rglob("*")):
                if entry.is_symlink():
                    raise RuntimeError(f"clean_build_generated_symlink_forbidden:{entry}")
                if entry.is_file():
                    actual_files.append(entry.relative_to(root).as_posix())
    expected_files = sorted(relative.as_posix() for relative in inputs)
    if sorted(actual_files) != expected_files:
        raise RuntimeError(
            f"clean_build_input_set_invalid:actual={sorted(actual_files)}:expected={expected_files}"
        )
    return {
        "generated_trees_unlinked": removed,
        "preserved_clean_build_inputs": reports,
        "generated_files_at_build_start": expected_files,
        "stale_generated_files_at_build_start": 0,
    }


def materialize_accepted_prepared_base(
    source: Path, destination: Path, expected_rows: list[dict[str, Any]],
) -> Path:
    """Ingest authenticated registry prepared bytes as a new immutable base.

    This is base production, NOT candidate/release materialization (which still
    must hardlink an immutable base). Never link, chmod or chown the report tree.
    The only privileged operation is the existing descriptor-bound owner seal;
    its allowed site roots are unchanged. Failed/uncertain seals are not retried
    or removed automatically. An existing target must independently qualify.
    """
    source = _normalized_absolute(source, "prepared_base")
    destination = _normalized_absolute(destination, "prepared_base_destination")
    registry = _CANONICAL_MACHINE_ROOT / "registry-site-sync"
    if (source.name != "release" or source.parent.name != "prepared"
            or source.parent.parent.parent != registry):
        raise PermissionError("accepted_prepared_source_outside_registry")
    _fixed_child_parent(destination, (_SITE_ROOT / "releases",), "accepted_prepared_base")
    indexed: dict[str, dict[str, Any]] = {}
    digest = hashlib.sha256()
    for row in sorted(expected_rows, key=lambda r: r["path"].encode()):
        path = row["path"]
        if (set(row) != {"path", "sha256", "bytes"} or not isinstance(path, str) or not path
                or path.startswith("/") or "\\" in path or "\0" in path
                or any(p in {"", ".", ".."} for p in path.split("/")) or path in indexed
                or type(row["bytes"]) is not int or row["bytes"] < 0 or not _valid_sha256(row["sha256"])):
            raise ValueError("accepted_prepared_inventory_invalid")
        indexed[path] = row
        digest.update(f"{path}\0{row['bytes']}\0{row['sha256']}\n".encode())
    if not indexed or destination.name != "accepted-prepared-" + digest.hexdigest():
        raise ValueError("accepted_prepared_destination_digest_invalid")
    source_fd = _open_tree_root(source)
    parent_fd = -1
    destination_fd = -1
    retained: list[int] = []
    previous_limit = None
    try:
        parent_fd = _open_tree_root(destination.parent)
        before = _tree_manifest_fd(source_fd, require_immutable=False)
        if set(before["files"]) != set(indexed):
            raise RuntimeError("accepted_prepared_source_inventory_drift")
        try:
            os.mkdir(destination.name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            created = False
        destination_fd = os.open(destination.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        if created:
            previous_limit = _raise_bounded_fd_limit(
                len(before["directories"]) + len(before["files"]) + 32, "accepted_prepared_base")
            retained.append(os.dup(destination_fd))

        def walk(source_dir: int, target_dir: int, prefix: Path) -> None:
            if set(os.listdir(source_dir)) != set(os.listdir(target_dir)) and not created:
                raise RuntimeError("accepted_prepared_existing_inventory_drift")
            for name in sorted(os.listdir(source_dir)):
                relative = (prefix / name).as_posix()
                info = _entry_stat(source_dir, name)
                if stat.S_ISDIR(info.st_mode):
                    child_source, _ = _opened_stat(source_dir, name, _DIRECTORY_FLAGS, info)
                    child_target = -1
                    try:
                        if created:
                            os.mkdir(name, 0o700, dir_fd=target_dir)
                        child_target = os.open(name, _DIRECTORY_FLAGS, dir_fd=target_dir)
                        if created:
                            retained.append(os.dup(child_target))
                        walk(child_source, child_target, prefix / name)
                    finally:
                        os.close(child_source)
                        if child_target >= 0:
                            os.close(child_target)
                elif stat.S_ISREG(info.st_mode):
                    row = indexed.get(relative)
                    if row is None:
                        raise RuntimeError("accepted_prepared_source_inventory_drift")
                    source_file, opened = _opened_stat(source_dir, name, _FILE_FLAGS, info)
                    target_file = -1
                    try:
                        flags = (os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW) if created else _FILE_FLAGS
                        target_file = os.open(name, flags, 0o600, dir_fd=target_dir)
                        if created:
                            retained.append(os.dup(target_file))
                            while True:
                                block = os.read(source_file, 1024 * 1024)
                                if not block:
                                    break
                                view = memoryview(block)
                                while view:
                                    written = os.write(target_file, view)
                                    view = view[written:]
                            os.fsync(target_file)
                            os.fchmod(target_file, 0o444 | (stat.S_IMODE(opened.st_mode) & 0o111))
                        target_info = os.fstat(target_file)
                        if ((opened.st_dev, opened.st_ino) == (target_info.st_dev, target_info.st_ino)
                                or target_info.st_size != row["bytes"] or _hash_fd(target_file) != row["sha256"]
                                or _hash_fd(source_file) != row["sha256"]
                                or _snapshot(os.fstat(source_file)) != _snapshot(opened)):
                            raise RuntimeError("accepted_prepared_content_drift")
                    finally:
                        os.close(source_file)
                        if target_file >= 0:
                            os.close(target_file)
                else:
                    raise ValueError("accepted_prepared_special_entry")

        walk(source_fd, destination_fd, Path())
        if _tree_manifest_fd(source_fd, require_immutable=False) != before:
            raise RuntimeError("accepted_prepared_source_descriptor_drift")
        if created:
            for fd in retained:
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    os.fchmod(fd, 0o555)
                os.fsync(fd)
            if os.geteuid() == 0:
                for fd in retained:
                    os.fchown(fd, 0, 0)
            else:
                _sudo_chown_fds(retained, 0, 0)
        final = _tree_manifest_fd(destination_fd, require_owner=(0, 0))
        if set(final["files"]) != set(indexed) or set(final["directories"]) != set(before["directories"]):
            raise RuntimeError("accepted_prepared_final_inventory_drift")
        for path, snapshot in final["files"].items():
            parent = _open_relative_directory_fd(destination_fd, str(Path(path).parent))
            file_fd = -1
            try:
                file_fd = os.open(Path(path).name, _FILE_FLAGS, dir_fd=parent)
                if _preserved_snapshot(os.fstat(file_fd)) != snapshot or _hash_fd(file_fd) != indexed[path]["sha256"]:
                    raise RuntimeError("accepted_prepared_final_content_drift")
            finally:
                if file_fd >= 0:
                    os.close(file_fd)
                os.close(parent)
        for path, fd in ((source, source_fd), (destination.parent, parent_fd)):
            rebound = _open_tree_root(path)
            try:
                if (os.fstat(rebound).st_dev, os.fstat(rebound).st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
                    raise RuntimeError("accepted_prepared_root_descriptor_drift")
            finally:
                os.close(rebound)
        if not _destination_binding_matches(parent_fd, destination.name, destination_fd):
            raise RuntimeError("accepted_prepared_destination_rebound")
        if _tree_manifest_fd(source_fd, require_immutable=False) != before:
            raise RuntimeError("accepted_prepared_source_descriptor_drift")
        return destination
    finally:
        for fd in retained:
            os.close(fd)
        if destination_fd >= 0:
            os.close(destination_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(source_fd)
        if previous_limit is not None:
            resource.setrlimit(resource.RLIMIT_NOFILE, previous_limit)


def copytree_linked(
    source: Path,
    destination: Path,
    *,
    ignore: IgnoreCallable | None = None,
) -> dict[str, Any]:
    """Materialize an immutable source with descriptor-bound hardlinks.

    Ordinary scheduler-owned fixtures use ``linkat`` through retained directory
    descriptors. A source containing foreign-owned entries enters the privileged
    path only when *every* entry is immutable ``root:root`` and both source and
    destination are immediate children of the fixed flagship release/candidate
    roots. Sudo receives only ``/proc/<pid>/fd/<n>`` paths, never caller paths.
    """
    source = _normalized_absolute(source, "linked_source")
    destination = _normalized_absolute(destination, "linked_destination")
    if source == destination:
        raise ValueError("linked_source_destination_identical")
    _assert_materialization_scope(source, destination)

    source_fd = _open_tree_root(source)
    destination_parent_fd: int | None = None
    destination_fd: int | None = None
    privileged = False
    created = False
    try:
        full_source_manifest = _tree_manifest_fd(source_fd)
        ignore_map = _ignore_map_fd(source_fd, source, ignore)
        source_manifest = _tree_manifest_fd(source_fd, ignore_map=ignore_map)
        owners = _manifest_owners(full_source_manifest)
        privileged = owners != {(os.geteuid(), os.getegid())}
        if privileged:
            _fixed_child_parent(source, _PRIVILEGED_SOURCE_PARENTS, "privileged_source")
            _fixed_child_parent(
                destination, _PRIVILEGED_DESTINATION_PARENTS, "privileged_destination"
            )
            # Re-run against the retained descriptor with the strict owner contract.
            full_source_manifest = _tree_manifest_fd(source_fd, require_owner=(0, 0))
            source_manifest = _tree_manifest_fd(
                source_fd,
                require_owner=(0, 0),
                ignore_map=ignore_map,
            )

        try:
            destination_parent_fd = _open_directory_nofollow(destination.parent)
        except OSError as exc:
            raise ValueError(
                f"linked_destination_symlink_ancestor_or_parent_invalid:{destination.parent}:{exc.errno}"
            ) from None
        try:
            os.stat(destination.name, dir_fd=destination_parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(f"linked_destination_exists:{destination}")

        source_device = os.fstat(source_fd).st_dev
        destination_device = os.fstat(destination_parent_fd).st_dev
        if source_device != destination_device:
            raise OSError(f"linked_cross_device_forbidden:{source_device}:{destination_device}")

        os.mkdir(destination.name, 0o700, dir_fd=destination_parent_fd)
        created = True
        destination_fd = os.open(destination.name, _DIRECTORY_FLAGS, dir_fd=destination_parent_fd)
        if not _destination_binding_matches(destination_parent_fd, destination.name, destination_fd):
            raise RuntimeError("linked_destination_binding_lost_after_create")

        if privileged:
            _sudo_chown_fds([destination_fd], 0, 0)
            if (os.fstat(destination_fd).st_uid, os.fstat(destination_fd).st_gid) != (0, 0):
                raise RuntimeError("privileged_destination_root_owner_invalid")
            _sudo_copy_between_fds(source_fd, destination_fd)
            _handoff_directories(
                destination_fd, os.geteuid(), os.getegid(), privileged=True
            )
            _remove_ignored_destination_entries(destination_fd, ignore_map)
            linked_files, linked_bytes = _verify_linked_files(source_manifest, destination_fd)
            copied_directories = len(source_manifest["directories"])
        else:
            linked_files, linked_bytes, copied_directories = _link_tree_direct(
                source_fd, destination_fd, ignore_map
            )
            verified_files, verified_bytes = _verify_linked_files(source_manifest, destination_fd)
            if (verified_files, verified_bytes) != (linked_files, linked_bytes):
                raise RuntimeError("linked_materialization_count_mismatch")
            copied_directories = _handoff_directories(
                destination_fd, os.geteuid(), os.getegid(), privileged=False
            )

        if not _destination_binding_matches(destination_parent_fd, destination.name, destination_fd):
            raise RuntimeError("linked_destination_binding_lost")
        if _tree_manifest_fd(
            source_fd,
            require_owner=(0, 0) if privileged else None,
        ) != full_source_manifest:
            raise RuntimeError("linked_source_changed_during_materialization")
        if linked_files != len(source_manifest["files"]) or copied_directories != len(
            source_manifest["directories"]
        ):
            raise RuntimeError("linked_materialization_count_mismatch")
        return {
            "source": str(source),
            "destination": str(destination),
            "linked_files": linked_files,
            "linked_bytes": linked_bytes,
            "copied_files": 0,
            "copied_bytes": 0,
            "copied_directories": copied_directories,
            "source_device": source_device,
            "destination_device": destination_device,
            "link_strategy": "privileged_descriptor_cp_al" if privileged else "descriptor_linkat",
            "ignored_paths": sorted(
                (Path(relative) / name).as_posix()
                for relative, names in ignore_map.items()
                for name in names
            ),
            "privileged_scope": (
                {
                    "source_parent": str(source.parent),
                    "destination_parent": str(destination.parent),
                    "source_owner": "root:root",
                    "raw_paths_passed_to_sudo": False,
                    "destination_descriptor_bound": True,
                }
                if privileged
                else None
            ),
        }
    except Exception as original_error:
        if created and destination_parent_fd is not None and destination_fd is not None:
            try:
                _cleanup_destination(
                    destination_parent_fd,
                    destination.name,
                    destination_fd,
                    privileged=privileged,
                )
            except Exception as cleanup_error:
                raise cleanup_error from original_error
        raise
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if destination_parent_fd is not None:
            os.close(destination_parent_fd)
        os.close(source_fd)


def _hash_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        block = os.read(fd, 1024 * 1024)
        if not block:
            return digest.hexdigest()
        digest.update(block)


def _xattr_acl_digest_fd(fd: int) -> str:
    """Hash every extended attribute, including POSIX ACL xattrs, by exact bytes."""
    digest = hashlib.sha256()
    names = sorted(os.listxattr(fd), key=os.fsencode)
    for name in names:
        encoded_name = os.fsencode(name)
        value = os.getxattr(fd, name)
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def detach_for_write(path: Path) -> dict[str, Any]:
    """Replace one regular file with a private owner-writable copy."""
    path = _normalized_absolute(path, "detach_path")
    parent_fd = _open_tree_root(path.parent)
    os.close(parent_fd)
    if not _lexists(path) or path.is_symlink():
        raise ValueError(f"detach_requires_regular_file:{path}")
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"detach_requires_regular_file:{path}")
    source_fd = os.open(path, _FILE_FLAGS)
    temporary: Path | None = None
    try:
        opened = os.fstat(source_fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError(f"detach_source_raced:{path}")
        temp_fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.detach-", dir=path.parent)
        temporary = Path(temp_name)
        digest = hashlib.sha256()
        try:
            os.lseek(source_fd, 0, os.SEEK_SET)
            while True:
                block = os.read(source_fd, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                view = memoryview(block)
                while view:
                    written = os.write(temp_fd, view)
                    view = view[written:]
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)
        shutil.copystat(path, temporary, follow_symlinks=False)
        os.chmod(
            temporary,
            stat.S_IMODE(before.st_mode) | stat.S_IWUSR,
            follow_symlinks=False,
        )
        os.replace(temporary, path)
        temporary = None

        after_original = os.fstat(source_fd)
        if _snapshot(after_original) != _snapshot(before):
            raise RuntimeError(f"detach_mutated_source_metadata:{path}")
        original_sha = _hash_fd(source_fd)
        if original_sha != digest.hexdigest():
            raise RuntimeError(f"detach_mutated_source_bytes:{path}")
        detached = os.lstat(path)
        if not stat.S_ISREG(detached.st_mode) or stat.S_ISLNK(detached.st_mode):
            raise RuntimeError(f"detach_result_not_regular:{path}")
        if (detached.st_dev, detached.st_ino) == (before.st_dev, before.st_ino):
            raise RuntimeError(f"detach_inode_unchanged:{path}")
        if not stat.S_IMODE(detached.st_mode) & stat.S_IWUSR:
            raise RuntimeError(f"detach_result_not_owner_writable:{path}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != original_sha:
            raise RuntimeError(f"detach_result_byte_mismatch:{path}")
        return {
            "path": str(path),
            "source_device": before.st_dev,
            "source_inode": before.st_ino,
            "detached_device": detached.st_dev,
            "detached_inode": detached.st_ino,
            "source_mode": f"{stat.S_IMODE(before.st_mode):04o}",
            "detached_mode": f"{stat.S_IMODE(detached.st_mode):04o}",
            "bytes": before.st_size,
            "sha256": original_sha,
        }
    finally:
        os.close(source_fd)
        if temporary is not None and _lexists(temporary):
            temporary.unlink()


def _normalized_relative_files(values: Sequence[Path], label: str) -> tuple[Path, ...]:
    normalized: list[Path] = []
    for raw in values:
        value = Path(raw)
        if (
            value.is_absolute()
            or not value.parts
            or value.as_posix() in {"", ".", ".."}
            or any(part in {"", ".", ".."} for part in value.parts)
        ):
            raise ValueError(f"{label}_relative_path_invalid:{value}")
        if value in normalized:
            raise ValueError(f"{label}_duplicate:{value}")
        normalized.append(value)
    return tuple(normalized)


def tree_content_closure(
    root: Path,
    *,
    ignore: IgnoreCallable | None = None,
) -> dict[str, Any]:
    """Hash one exact regular-file tree with descriptor-bound identity checks."""
    root = _normalized_absolute(root, "closure_root")
    root_fd = _open_tree_root(root)
    try:
        ignore_map = _ignore_map_fd(root_fd, root, ignore)
        manifest = _tree_manifest_fd(root_fd, require_immutable=False, ignore_map=ignore_map)
        rows: list[dict[str, Any]] = []
        for relative, expected in sorted(manifest["files"].items()):
            parent_relative = Path(relative).parent.as_posix()
            parent_fd = _open_relative_directory_fd(
                root_fd,
                "." if parent_relative == "." else parent_relative,
            )
            try:
                name = Path(relative).name
                info = _entry_stat(parent_fd, name)
                file_fd, opened = _opened_stat(parent_fd, name, _FILE_FLAGS, info)
                try:
                    if _preserved_snapshot(opened) != expected:
                        raise RuntimeError(f"closure_entry_identity_changed:{relative}")
                    xattr_acl_digest = _xattr_acl_digest_fd(file_fd)
                    digest = _hash_fd(file_fd)
                    after = os.fstat(file_fd)
                    if _preserved_snapshot(after) != expected:
                        raise RuntimeError(f"closure_entry_identity_changed:{relative}")
                    if _xattr_acl_digest_fd(file_fd) != xattr_acl_digest:
                        raise RuntimeError(f"closure_entry_xattr_changed:{relative}")
                finally:
                    os.close(file_fd)
            finally:
                os.close(parent_fd)
            rows.append({
                "path": relative,
                "sha256": digest,
                "bytes": expected[-1],
                "mode": f"{stat.S_IMODE(expected[2]):04o}",
                "uid": expected[3],
                "gid": expected[4],
                "mtime_ns": expected[5],
                "xattr_acl_sha256": xattr_acl_digest,
            })
        closure_digest = hashlib.sha256()
        for row in rows:
            closure_digest.update(
                (
                    f"{row['path']}\0{row['sha256']}\0{row['bytes']}\0{row['mode']}\0"
                    f"{row['uid']}\0{row['gid']}\0{row['mtime_ns']}\0"
                    f"{row['xattr_acl_sha256']}\n"
                ).encode("utf-8")
            )
        return {
            "tree_sha256": closure_digest.hexdigest(),
            "files": len(rows),
            "bytes": sum(int(row["bytes"]) for row in rows),
            "rows": rows,
        }
    finally:
        os.close(root_fd)


def verify_cow_topology(
    predecessor: Path,
    output: Path,
    mutation_paths: Sequence[Path],
    *,
    ignore: IgnoreCallable | None = None,
) -> dict[str, Any]:
    """Reject full-copy outputs and any write scope outside declared CoW paths."""
    predecessor = _normalized_absolute(predecessor, "cow_predecessor")
    output = _normalized_absolute(output, "cow_output")
    mutations = _normalized_relative_files(mutation_paths, "cow_mutation")
    mutation_names = {path.as_posix() for path in mutations}
    before = tree_content_closure(predecessor, ignore=ignore)
    after = tree_content_closure(output, ignore=ignore)
    before_rows = {row["path"]: row for row in before["rows"]}
    after_rows = {row["path"]: row for row in after["rows"]}
    added = sorted(set(after_rows) - set(before_rows))
    deleted = sorted(set(before_rows) - set(after_rows))
    modified = sorted(
        path
        for path in set(before_rows) & set(after_rows)
        if before_rows[path] != after_rows[path]
    )
    undeclared = sorted((set(added) | set(modified)) - mutation_names)
    if deleted:
        raise RuntimeError(f"cow_output_deleted_paths:{deleted}")
    if undeclared:
        raise RuntimeError(f"cow_output_undeclared_mutation:{undeclared}")

    unchanged_shared = 0
    private_mutations = 0
    for relative in sorted(set(before_rows) & set(after_rows)):
        source_info = os.lstat(predecessor / relative)
        output_info = os.lstat(output / relative)
        same_inode = (source_info.st_dev, source_info.st_ino) == (
            output_info.st_dev,
            output_info.st_ino,
        )
        if relative in mutation_names:
            if same_inode:
                raise RuntimeError(f"cow_mutation_not_detached:{relative}")
            private_mutations += 1
        else:
            if before_rows[relative] != after_rows[relative] or not same_inode:
                raise RuntimeError(f"cow_unchanged_path_not_shared:{relative}")
            unchanged_shared += 1
    for relative in added:
        if relative not in mutation_names:
            raise RuntimeError(f"cow_added_path_undeclared:{relative}")
        private_mutations += 1

    return {
        "status": "PASS_HARDLINK_COW_TOPOLOGY",
        "predecessor": {
            "path": str(predecessor),
            "tree_sha256": before["tree_sha256"],
            "files": before["files"],
            "bytes": before["bytes"],
        },
        "output": {
            "path": str(output),
            "tree_sha256": after["tree_sha256"],
            "files": after["files"],
            "bytes": after["bytes"],
        },
        "added_paths": added,
        "modified_paths": modified,
        "deleted_paths": deleted,
        "declared_mutation_paths": sorted(mutation_names),
        "unchanged_shared_files": unchanged_shared,
        "private_mutation_files": private_mutations,
        "violations": [],
    }


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _attested_file_sha256(path: Path, label: str) -> str:
    """Hash one exact live regular file without accepting a symlink identity."""
    path = _normalized_absolute(path, label)
    try:
        if path.resolve(strict=True) != path:
            raise RuntimeError
        before = os.lstat(path)
    except (OSError, RuntimeError):
        raise RuntimeError(f"constructor_provenance_{label}_live_file_invalid") from None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"constructor_provenance_{label}_live_file_invalid")
    fd = os.open(path, _FILE_FLAGS)
    try:
        opened = os.fstat(fd)
        if _preserved_snapshot(opened) != _preserved_snapshot(before):
            raise RuntimeError(f"constructor_provenance_{label}_live_file_changed")
        digest = _hash_fd(fd)
        if _preserved_snapshot(os.fstat(fd)) != _preserved_snapshot(before):
            raise RuntimeError(f"constructor_provenance_{label}_live_file_changed")
        rebound = os.lstat(path)
        if _preserved_snapshot(rebound) != _preserved_snapshot(before):
            raise RuntimeError(f"constructor_provenance_{label}_live_file_changed")
    finally:
        os.close(fd)
    return digest


def _materialization_phases(
    materializations: Sequence[dict[str, Any]],
    predecessor: Path,
    output: Path,
) -> list[str]:
    """Bind every retained copytree edge to its exact construction phase."""
    sources = [Path(str(row.get("source", ""))) for row in materializations]
    destinations = [Path(str(row.get("destination", ""))) for row in materializations]
    if len(materializations) == 1:
        if sources[0] == predecessor and destinations[0] == output:
            return ["predecessor_to_output"]
        if (
            sources[0] == predecessor.parent
            and predecessor.name == output.name
            and _linked_restore_destination_matches(destinations[0], output)
        ):
            return ["linked_subtree_restore_staging"]
    elif len(materializations) == 2:
        if (
            sources[0] == predecessor
            and sources[1] == destinations[0]
            and destinations[1].parent == output.parent
            and destinations[1].name.startswith(f".{output.name}.tmp-")
        ):
            return ["predecessor_to_candidate", "candidate_to_output_staging"]
    raise RuntimeError("constructor_provenance_materialization_edge_phase_invalid")


def _linked_restore_destination_matches(destination: Path, output: Path) -> bool:
    """Recognize the exact staging edge before or after candidate-root rename."""
    if destination.parent != output.parent.parent:
        return False
    if destination.name.startswith(f".{output.parent.name}.linked-restore-"):
        return True
    prefix = f"..candidate.cow-{output.parent.name}-"
    if not destination.name.startswith(prefix):
        return False
    remainder = destination.name[len(prefix):]
    parts = remainder.split(".linked-restore-", 1)
    if len(parts) != 2:
        return False

    def valid_pid_token(value: str) -> bool:
        fields = value.split("-", 1)
        return (
            len(fields) == 2
            and fields[0].isdigit()
            and len(fields[1]) == 16
            and all(character in "0123456789abcdef" for character in fields[1])
        )

    return all(valid_pid_token(value) for value in parts)


def _materialization_rows(
    materializations: Sequence[dict[str, Any]],
    predecessor: Path,
    output: Path,
) -> list[dict[str, Any]]:
    raw_rows = []
    for row in materializations:
        cleaned = dict(row)
        cleaned.pop("api", None)
        cleaned.pop("phase", None)
        raw_rows.append(cleaned)
    phases = _materialization_phases(raw_rows, predecessor, output)
    return [
        {
            "api": "site_tree_materializer.copytree_linked",
            "phase": phase,
            **row,
        }
        for phase, row in zip(phases, raw_rows, strict=True)
    ]


def validate_constructor_provenance_structure(value: dict[str, Any]) -> dict[str, Any]:
    """Validate receipt shape only; this is deliberately not live attestation."""
    required = {
        "schema", "constructor", "materializer", "policy", "materializations",
        "predecessor", "output", "mutation_paths", "ignored_names", "topology",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise RuntimeError("constructor_provenance_fields_invalid")
    if value.get("schema") != CONSTRUCTOR_PROVENANCE_SCHEMA:
        raise RuntimeError("constructor_provenance_schema_invalid")
    if value.get("policy") != HARDLINK_COW_POLICY:
        raise RuntimeError("constructor_provenance_topology_policy_invalid")
    materializer = value.get("materializer")
    materializer_path = Path(__file__).resolve()
    if (
        not isinstance(materializer, dict)
        or set(materializer) != {"path", "sha256"}
        or materializer.get("path") != str(materializer_path)
        or not _valid_sha256(materializer.get("sha256"))
    ):
        raise RuntimeError("constructor_provenance_materializer_invalid")
    constructor = value.get("constructor")
    if not isinstance(constructor, dict) or set(constructor) != {"path", "sha256"}:
        raise RuntimeError("constructor_provenance_constructor_invalid")
    constructor_path = Path(str(constructor.get("path", "")))
    if (
        not constructor_path.is_absolute()
        or not _valid_sha256(constructor.get("sha256"))
    ):
        raise RuntimeError("constructor_provenance_constructor_invalid")
    materializations = value.get("materializations")
    if not isinstance(materializations, list) or not materializations:
        raise RuntimeError("constructor_provenance_materialization_missing")
    for row in materializations:
        if (
            not isinstance(row, dict)
            or row.get("api") != "site_tree_materializer.copytree_linked"
            or row.get("phase") not in {
                "predecessor_to_output",
                "predecessor_to_candidate",
                "candidate_to_output_staging",
                "linked_subtree_restore_staging",
            }
            or not isinstance(row.get("source"), str)
            or not Path(row["source"]).is_absolute()
            or not isinstance(row.get("destination"), str)
            or not Path(row["destination"]).is_absolute()
            or type(row.get("linked_files")) is not int
            or row["linked_files"] <= 0
            or row.get("copied_files") != 0
            or row.get("copied_bytes") != 0
            or row.get("source_device") != row.get("destination_device")
            or row.get("link_strategy") not in {"descriptor_linkat", "privileged_descriptor_cp_al"}
        ):
            raise RuntimeError("constructor_provenance_full_copy_or_materialization_invalid")
    predecessor_record = value.get("predecessor")
    if (
        not isinstance(predecessor_record, dict)
        or set(predecessor_record) != {"path", "tree_sha256_before", "tree_sha256_after", "unchanged"}
        or predecessor_record.get("unchanged") is not True
        or predecessor_record.get("tree_sha256_before") != predecessor_record.get("tree_sha256_after")
    ):
        raise RuntimeError("constructor_provenance_predecessor_invalid")
    topology = value.get("topology")
    ignored_names = value.get("ignored_names")
    if (
        not isinstance(ignored_names, list)
        or ignored_names != sorted(set(ignored_names))
        or any(not isinstance(name, str) or not name or "/" in name or "\\" in name for name in ignored_names)
    ):
        raise RuntimeError("constructor_provenance_ignored_names_invalid")
    if (
        not isinstance(topology, dict)
        or topology.get("status") != "PASS_HARDLINK_COW_TOPOLOGY"
        or topology.get("violations") != []
        or topology.get("deleted_paths") != []
        or topology.get("declared_mutation_paths") != value.get("mutation_paths")
        or topology.get("output") != value.get("output")
    ):
        raise RuntimeError("constructor_provenance_topology_invalid")
    return value


def validate_constructor_provenance(
    value: dict[str, Any],
    *,
    predecessor: Path | None = None,
    output: Path | None = None,
    constructor: Path | None = None,
) -> dict[str, Any]:
    """Authority-grade validation against exact live roots, code hashes, and edges."""
    validate_constructor_provenance_structure(value)
    if predecessor is None or output is None or constructor is None:
        raise RuntimeError("constructor_provenance_live_attestation_inputs_required")
    predecessor = _normalized_absolute(predecessor, "provenance_predecessor")
    output = _normalized_absolute(output, "provenance_output")
    constructor = _normalized_absolute(constructor, "provenance_constructor")
    predecessor_fd = _open_tree_root(predecessor)
    output_fd = _open_tree_root(output)
    os.close(predecessor_fd)
    os.close(output_fd)

    predecessor_record = value["predecessor"]
    if predecessor_record.get("path") != str(predecessor):
        raise RuntimeError("constructor_provenance_predecessor_path_invalid")
    if value["output"].get("path") != str(output):
        raise RuntimeError("constructor_provenance_output_path_invalid")
    if value["constructor"].get("path") != str(constructor):
        raise RuntimeError("constructor_provenance_constructor_path_invalid")
    if _attested_file_sha256(constructor, "constructor") != value["constructor"]["sha256"]:
        raise RuntimeError("constructor_provenance_constructor_hash_mismatch")
    materializer_path = Path(__file__).resolve()
    if _attested_file_sha256(materializer_path, "materializer") != value["materializer"]["sha256"]:
        raise RuntimeError("constructor_provenance_materializer_hash_mismatch")

    expected_phases = _materialization_phases(value["materializations"], predecessor, output)
    if [row["phase"] for row in value["materializations"]] != expected_phases:
        raise RuntimeError("constructor_provenance_materialization_edge_phase_invalid")
    ignored_names = value["ignored_names"]
    ignore = shutil.ignore_patterns(*ignored_names) if ignored_names else None
    live = verify_cow_topology(
        predecessor,
        output,
        [Path(path) for path in value["mutation_paths"]],
        ignore=ignore,
    )
    if live != value["topology"]:
        raise RuntimeError("constructor_provenance_live_topology_drift")
    current_predecessor = tree_content_closure(predecessor, ignore=ignore)
    if current_predecessor["tree_sha256"] != predecessor_record["tree_sha256_after"]:
        raise RuntimeError("constructor_provenance_predecessor_drift")
    return value


def constructor_provenance(
    *,
    constructor: Path,
    predecessor: Path,
    output: Path,
    mutation_paths: Sequence[Path],
    materializations: Sequence[dict[str, Any]],
    predecessor_before: dict[str, Any],
    ignored_names: Sequence[str] = (),
) -> dict[str, Any]:
    predecessor = _normalized_absolute(predecessor, "constructor_predecessor")
    output = _normalized_absolute(output, "constructor_output")
    constructor = _normalized_absolute(constructor, "constructor_path")
    ignored = sorted(set(ignored_names))
    if any(not isinstance(name, str) or not name or "/" in name or "\\" in name for name in ignored):
        raise ValueError("constructor_ignored_name_invalid")
    ignore = shutil.ignore_patterns(*ignored) if ignored else None
    predecessor_after = tree_content_closure(predecessor, ignore=ignore)
    if predecessor_before.get("tree_sha256") != predecessor_after["tree_sha256"]:
        raise RuntimeError("constructor_predecessor_changed")
    topology = verify_cow_topology(predecessor, output, mutation_paths, ignore=ignore)
    materialization_rows = _materialization_rows(materializations, predecessor, output)
    value = {
        "schema": CONSTRUCTOR_PROVENANCE_SCHEMA,
        "constructor": {
            "path": str(constructor),
            "sha256": hashlib.sha256(constructor.read_bytes()).hexdigest(),
        },
        "materializer": {
            "path": str(Path(__file__).resolve()),
            "sha256": hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest(),
        },
        "policy": dict(HARDLINK_COW_POLICY),
        "materializations": materialization_rows,
        "predecessor": {
            "path": str(predecessor),
            "tree_sha256_before": predecessor_before["tree_sha256"],
            "tree_sha256_after": predecessor_after["tree_sha256"],
            "unchanged": True,
        },
        "output": topology["output"],
        "mutation_paths": topology["declared_mutation_paths"],
        "ignored_names": ignored,
        "topology": topology,
    }
    return validate_constructor_provenance(
        value,
        predecessor=predecessor,
        output=output,
        constructor=constructor,
    )


def _write_private_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, int(payload["mode"]) | stat.S_IWUSR)
    try:
        view = memoryview(payload["bytes_value"])
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise RuntimeError("linked_restore_short_write")
            view = view[written:]
        os.fsync(fd)
        os.fchmod(fd, int(payload["mode"]) | stat.S_IWUSR)
    finally:
        os.close(fd)
    os.utime(
        path,
        ns=(int(payload["atime_ns"]), int(payload["mtime_ns"])),
        follow_symlinks=False,
    )


def restore_linked_subtree(
    predecessor_root: Path,
    candidate_root: Path,
    subtree: Path,
    mutation_paths: Sequence[Path],
    *,
    constructor: Path,
) -> dict[str, Any]:
    """Restore a generated subtree as base links plus private declared overlays."""
    predecessor_root = _normalized_absolute(predecessor_root, "restore_predecessor")
    candidate_root = _normalized_absolute(candidate_root, "restore_candidate")
    subtree_values = _normalized_relative_files((subtree,), "restore_subtree")
    subtree = subtree_values[0]
    mutations = _normalized_relative_files(mutation_paths, "restore_mutation")
    predecessor_subtree = predecessor_root / subtree
    candidate_subtree = candidate_root / subtree
    if not predecessor_subtree.is_dir() or predecessor_subtree.is_symlink():
        raise RuntimeError(f"restore_predecessor_subtree_invalid:{subtree}")
    if not candidate_subtree.is_dir() or candidate_subtree.is_symlink():
        raise RuntimeError(f"restore_candidate_subtree_invalid:{subtree}")

    payloads: dict[Path, dict[str, Any]] = {}
    for relative in mutations:
        source = candidate_subtree / relative
        if not _lexists(source) or source.is_symlink() or not source.is_file():
            raise RuntimeError(f"restore_declared_output_missing:{relative}")
        info = os.lstat(source)
        payloads[relative] = {
            "bytes_value": source.read_bytes(),
            "mode": stat.S_IMODE(info.st_mode),
            "atime_ns": info.st_atime_ns,
            "mtime_ns": info.st_mtime_ns,
        }

    predecessor_before = tree_content_closure(predecessor_subtree)
    token = secrets.token_hex(8)
    staged_root = candidate_root.with_name(
        f".{candidate_root.name}.linked-restore-{os.getpid()}-{token}"
    )
    backup = candidate_root / f".{subtree.name}.generated-backup-{token}"
    if _lexists(staged_root) or _lexists(backup):
        raise RuntimeError("linked_restore_temporary_collision")
    materialization: dict[str, Any] | None = None
    swapped = False
    try:
        materialization = copytree_linked(predecessor_root, staged_root)
        staged_subtree = staged_root / subtree
        detachments: list[dict[str, Any]] = []
        additions: list[str] = []
        for relative, payload in payloads.items():
            destination = staged_subtree / relative
            if _lexists(destination):
                detachments.append(detach_for_write(destination))
                destination.unlink()
            else:
                additions.append(relative.as_posix())
            _write_private_payload(destination, payload)
            if destination.read_bytes() != payload["bytes_value"]:
                raise RuntimeError(f"linked_restore_payload_mismatch:{relative}")

        os.replace(candidate_subtree, backup)
        try:
            os.replace(staged_subtree, candidate_subtree)
            swapped = True
        except Exception:
            os.replace(backup, candidate_subtree)
            raise

        provenance = constructor_provenance(
            constructor=constructor,
            predecessor=predecessor_subtree,
            output=candidate_subtree,
            mutation_paths=mutations,
            materializations=[materialization],
            predecessor_before=predecessor_before,
        )
        remove_materialized_tree(backup)
        swapped = False
        return {
            "status": "PASS_LINKED_SUBTREE_RESTORE",
            "subtree": subtree.as_posix(),
            "materialization": materialization,
            "detachments": detachments,
            "private_additions": sorted(additions),
            "constructor_provenance": provenance,
        }
    except Exception:
        if swapped:
            try:
                remove_materialized_tree(candidate_subtree)
                os.replace(backup, candidate_subtree)
            except Exception as rollback_error:
                raise RuntimeError(f"linked_restore_rollback_failed:{rollback_error}") from None
        raise
    finally:
        remove_materialized_tree(staged_root)
        if _lexists(backup) and not swapped:
            remove_materialized_tree(backup)


def _chunks(paths: Sequence[Path], maximum_items: int = 100) -> Iterable[Sequence[Path]]:
    for start in range(0, len(paths), maximum_items):
        yield paths[start:start + maximum_items]


def _set_owner(paths: Sequence[Path], uid: int, gid: int, *, use_sudo: bool) -> int:
    pending = [path for path in paths if (os.lstat(path).st_uid, os.lstat(path).st_gid) != (uid, gid)]
    if not pending:
        return 0
    if os.geteuid() == 0:
        for path in pending:
            os.chown(path, uid, gid, follow_symlinks=False)
    elif use_sudo:
        previous_limit = _raise_bounded_fd_limit(len(pending), "tree_owner")
        fds: list[int] = []
        try:
            for path in pending:
                path = _normalized_absolute(path, "owner_path")
                parent_fd = _open_tree_root(path.parent)
                try:
                    flags = getattr(os, "O_PATH", os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0)
                    fd = os.open(path.name, flags, dir_fd=parent_fd)
                finally:
                    os.close(parent_fd)
                info = os.fstat(fd)
                if stat.S_ISLNK(info.st_mode):
                    os.close(fd)
                    raise ValueError(f"owner_symlink_forbidden:{path}")
                fds.append(fd)
            _sudo_chown_fds(fds, uid, gid)
        finally:
            for fd in fds:
                os.close(fd)
            resource.setrlimit(resource.RLIMIT_NOFILE, previous_limit)
    else:
        for path in pending:
            os.chown(path, uid, gid, follow_symlinks=False)
    return len(pending)


def seal_tree_selective(
    root: Path,
    immutable_bases: Sequence[Path],
    *,
    file_mode: int = 0o444,
    directory_mode: int = 0o555,
    owner: tuple[int, int] | None = (0, 0),
    use_sudo: bool = False,
) -> dict[str, Any]:
    """Seal private entries while preserving every base-linked file inode."""
    root = _normalized_absolute(root, "seal_root")
    if use_sudo and os.geteuid() != 0:
        _fixed_child_parent(root, _PRIVILEGED_DESTINATION_PARENTS, "privileged_seal")
    directories, files = _regular_files_and_directories(root)
    base_identities: set[tuple[int, int]] = set()
    for base in immutable_bases:
        _base_directories, base_files = _validate_immutable_base(Path(base))
        base_identities.update((os.lstat(path).st_dev, os.lstat(path).st_ino) for path in base_files)

    shared: list[Path] = []
    private: list[Path] = []
    shared_snapshots: dict[Path, tuple[int, ...]] = {}
    for path in files:
        info = os.lstat(path)
        if (info.st_dev, info.st_ino) in base_identities:
            if stat.S_IMODE(info.st_mode) & 0o222:
                raise RuntimeError(f"shared_base_file_writable:{path}")
            if owner is not None and (info.st_uid, info.st_gid) != owner:
                raise RuntimeError(f"shared_base_owner_invalid:{path}")
            shared.append(path)
            shared_snapshots[path] = _snapshot(info)
        else:
            private.append(path)

    for path in private:
        os.chmod(path, file_mode, follow_symlinks=False)
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        os.chmod(path, directory_mode, follow_symlinks=False)

    owner_changes = 0
    if owner is not None:
        owner_changes = _set_owner([*private, *directories], owner[0], owner[1], use_sudo=use_sudo)

    for path, before in shared_snapshots.items():
        if _snapshot(os.lstat(path)) != before:
            raise RuntimeError(f"selective_seal_mutated_shared_metadata:{path}")
    for path in private:
        info = os.lstat(path)
        if stat.S_IMODE(info.st_mode) != file_mode:
            raise RuntimeError(f"selective_seal_file_mode_invalid:{path}")
        if owner is not None and (info.st_uid, info.st_gid) != owner:
            raise RuntimeError(f"selective_seal_file_owner_invalid:{path}")
    for path in directories:
        info = os.lstat(path)
        if stat.S_IMODE(info.st_mode) != directory_mode:
            raise RuntimeError(f"selective_seal_directory_mode_invalid:{path}")
        if owner is not None and (info.st_uid, info.st_gid) != owner:
            raise RuntimeError(f"selective_seal_directory_owner_invalid:{path}")

    return {
        "root": str(root),
        "shared_files_preserved": len(shared),
        "private_files_sealed": len(private),
        "directories_sealed": len(directories),
        "owner_changes": owner_changes,
        "file_mode": f"{file_mode:04o}",
        "directory_mode": f"{directory_mode:04o}",
        "owner": list(owner) if owner is not None else None,
    }
