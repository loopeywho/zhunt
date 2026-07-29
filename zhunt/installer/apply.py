"""Apply and restore app configuration from packaged YAML recipes."""

from __future__ import annotations

import json
import os
import platform
import shutil
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from io import StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import tomlkit
import yaml
from ruamel.yaml import YAML


class InstallerError(ValueError):
    """Raised when a recipe cannot be applied safely."""


@dataclass(frozen=True)
class InstallationResult:
    app: str
    target: Path | None
    backup: Path | None
    manual_instructions: tuple[str, ...] = ()

    @property
    def manual(self) -> bool:
        return bool(self.manual_instructions)


class Installer:
    def __init__(
        self,
        *,
        home: Path | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.home = (home or Path.home()).resolve()
        self.platform_name = (
            platform_name or platform.system()
        ).lower()

    def install(
        self,
        app: str,
        *,
        mode: str,
        base_url: str,
    ) -> InstallationResult:
        recipe = _load_recipe(app)
        context = _template_context(base_url)
        if recipe.get("format") == "manual":
            instructions = tuple(
                _render(item, context)
                for item in recipe.get("instructions", [])
            )
            return InstallationResult(
                app=app,
                target=None,
                backup=None,
                manual_instructions=instructions,
            )

        mode_recipe = _mode_recipe(recipe, mode)
        if mode_recipe.get("supported", True) is not True:
            message = mode_recipe.get(
                "message",
                f"{mode} mode is not supported for {app}",
            )
            raise InstallerError(str(message))

        target = self._target(recipe)
        manifest_path = self._manifest_path(app)
        manifest = self._load_manifest(manifest_path)
        backup = None
        if manifest is None:
            backup = self._backup(app, target)
            manifest = {
                "app": app,
                "target": str(target),
                "original_existed": target.exists(),
                "backup": str(backup) if backup is not None else None,
            }
            _write_json(manifest_path, manifest)
        elif manifest.get("target") != str(target):
            raise InstallerError(
                f"recorded target for {app} does not match this platform"
            )
        elif isinstance(manifest.get("backup"), str):
            backup = Path(manifest["backup"])

        document = _read_document(
            target,
            recipe_format=str(recipe["format"]),
            root=str(recipe.get("root", "mapping")),
        )
        operations = _render(
            mode_recipe.get("operations", []),
            context,
        )
        _apply_operations(document, operations)
        _write_document(
            target,
            document,
            recipe_format=str(recipe["format"]),
        )
        return InstallationResult(app=app, target=target, backup=backup)

    def uninstall(self, app: str) -> InstallationResult:
        recipe = _load_recipe(app)
        if recipe.get("format") == "manual":
            instructions = tuple(
                str(item)
                for item in recipe.get("uninstall_instructions", [])
            )
            return InstallationResult(
                app=app,
                target=None,
                backup=None,
                manual_instructions=instructions,
            )

        manifest_path = self._manifest_path(app)
        manifest = self._load_manifest(manifest_path)
        if manifest is None:
            raise InstallerError(f"{app} is not installed by Zhunt")

        target = Path(str(manifest["target"]))
        backup_value = manifest.get("backup")
        backup = Path(backup_value) if isinstance(backup_value, str) else None
        if manifest.get("original_existed") is True:
            if backup is None or not backup.is_file():
                raise InstallerError(f"backup for {app} is missing")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(backup, target)
        elif target.exists():
            target.unlink()
        manifest_path.unlink()
        return InstallationResult(app=app, target=target, backup=backup)

    def _target(self, recipe: Mapping[str, Any]) -> Path:
        raw_targets = recipe.get("target")
        if not isinstance(raw_targets, Mapping):
            raise InstallerError("recipe target must be a mapping")
        relative = raw_targets.get(
            self.platform_name,
            raw_targets.get("default"),
        )
        if not isinstance(relative, str):
            raise InstallerError(
                f"recipe has no target for {self.platform_name}"
            )
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise InstallerError("recipe target must stay within the home directory")
        return self.home / path

    def _manifest_path(self, app: str) -> Path:
        return self.home / ".zhunt" / "installs" / f"{app}.json"

    def _load_manifest(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise InstallerError(f"invalid install manifest: {path}")
        return loaded

    def _backup(self, app: str, target: Path) -> Path | None:
        if not target.exists():
            return None
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = (
            self.home
            / ".zhunt"
            / "backups"
            / app
            / f"{timestamp}-{target.name}"
        )
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, backup)
        return backup


def supported_apps() -> tuple[str, ...]:
    recipes = resources.files("zhunt.installer").joinpath("recipes")
    return tuple(
        sorted(
            item.name.removesuffix(".yaml")
            for item in recipes.iterdir()
            if item.name.endswith(".yaml")
        )
    )


def _load_recipe(app: str) -> dict[str, Any]:
    if app not in supported_apps():
        raise InstallerError(f"unknown app: {app}")
    resource = resources.files("zhunt.installer").joinpath(
        "recipes",
        f"{app}.yaml",
    )
    with resource.open(encoding="utf-8") as recipe_file:
        loaded = yaml.safe_load(recipe_file)
    if not isinstance(loaded, dict):
        raise InstallerError(f"invalid recipe: {app}")
    return loaded


def _mode_recipe(
    recipe: Mapping[str, Any],
    mode: str,
) -> Mapping[str, Any]:
    modes = recipe.get("modes")
    if not isinstance(modes, Mapping) or mode not in modes:
        raise InstallerError(f"recipe does not define {mode} mode")
    selected = modes[mode]
    if not isinstance(selected, Mapping):
        raise InstallerError(f"invalid {mode} mode recipe")
    return selected


def _template_context(base_url: str) -> dict[str, str]:
    root = base_url.rstrip("/")
    if not root.startswith(("http://", "https://")):
        raise InstallerError("base URL must start with http:// or https://")
    return {
        "base_url": root,
        "openai_base_url": f"{root}/v1",
        "chat_endpoint": f"{root}/v1/chat/completions",
    }


def _render(value: Any, context: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        rendered = value
        for key, replacement in context.items():
            rendered = rendered.replace(f"{{{{ {key} }}}}", replacement)
        return rendered
    if isinstance(value, list):
        return [_render(item, context) for item in value]
    if isinstance(value, Mapping):
        return {
            key: _render(item, context)
            for key, item in value.items()
        }
    return value


def _read_document(
    path: Path,
    *,
    recipe_format: str,
    root: str,
) -> Any:
    if not path.exists():
        return [] if root == "list" else {}
    text = path.read_text(encoding="utf-8")
    if recipe_format == "json":
        return json.loads(text)
    if recipe_format == "toml":
        return tomlkit.parse(text)
    if recipe_format == "yaml":
        parser = YAML()
        loaded = parser.load(text)
        return loaded if loaded is not None else {}
    raise InstallerError(f"unsupported recipe format: {recipe_format}")


def _write_document(
    path: Path,
    document: Any,
    *,
    recipe_format: str,
) -> None:
    if recipe_format == "json":
        text = json.dumps(document, indent=2) + "\n"
    elif recipe_format == "toml":
        text = tomlkit.dumps(document)
    elif recipe_format == "yaml":
        output = StringIO()
        emitter = YAML()
        emitter.indent(mapping=2, sequence=4, offset=2)
        emitter.dump(document, output)
        text = output.getvalue()
    else:
        raise InstallerError(f"unsupported recipe format: {recipe_format}")
    _atomic_write(path, text)


def _apply_operations(document: Any, operations: Any) -> None:
    if not isinstance(operations, list):
        raise InstallerError("recipe operations must be a list")
    for operation in operations:
        if not isinstance(operation, Mapping):
            raise InstallerError("recipe operation must be a mapping")
        action = operation.get("action")
        path = operation.get("path", [])
        if not isinstance(path, list) or not all(
            isinstance(part, str) for part in path
        ):
            raise InstallerError("operation path must contain strings")
        if action == "set":
            _set_path(document, path, operation.get("value"))
        elif action == "merge":
            target = _mapping_at(document, path)
            value = operation.get("value")
            if not isinstance(value, Mapping):
                raise InstallerError("merge value must be a mapping")
            _deep_merge(target, value)
        elif action == "upsert_list":
            target = _list_at(document, path)
            match = operation.get("match")
            value = operation.get("value")
            if not isinstance(match, Mapping) or not isinstance(value, Mapping):
                raise InstallerError("upsert_list requires match and value mappings")
            _upsert_list(target, match, value)
        else:
            raise InstallerError(f"unsupported recipe action: {action}")


def _set_path(document: Any, path: list[str], value: Any) -> None:
    if not path:
        raise InstallerError("set requires a non-empty path")
    parent = _mapping_at(document, path[:-1])
    parent[path[-1]] = value


def _mapping_at(document: Any, path: list[str]) -> MutableMapping[str, Any]:
    target = _value_at(document, path, create=True)
    if not isinstance(target, MutableMapping):
        raise InstallerError("operation target must be a mapping")
    return target


def _value_at(
    document: Any,
    path: list[str],
    *,
    create: bool = False,
) -> Any:
    target = document
    for part in path:
        if not isinstance(target, MutableMapping):
            raise InstallerError("operation path crosses a non-mapping value")
        if part not in target:
            if not create:
                raise InstallerError(f"operation path does not exist: {part}")
            target[part] = {}
        target = target[part]
    return target


def _list_at(document: Any, path: list[str]) -> list[Any]:
    if not path:
        if not isinstance(document, list):
            raise InstallerError("upsert_list target must be a list")
        return document
    parent = _mapping_at(document, path[:-1])
    value = parent.get(path[-1])
    if value is None:
        value = []
        parent[path[-1]] = value
    if not isinstance(value, list):
        raise InstallerError("upsert_list target must be a list")
    return value


def _deep_merge(
    target: MutableMapping[str, Any],
    value: Mapping[str, Any],
) -> None:
    for key, item in value.items():
        existing = target.get(key)
        if isinstance(existing, MutableMapping) and isinstance(item, Mapping):
            _deep_merge(existing, item)
        else:
            target[key] = item


def _upsert_list(
    target: list[Any],
    match: Mapping[str, Any],
    value: Mapping[str, Any],
) -> None:
    for index, item in enumerate(target):
        if isinstance(item, Mapping) and all(
            item.get(key) == expected
            for key, expected in match.items()
        ):
            target[index] = value
            return
    target.append(value)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write(path, json.dumps(value, indent=2) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as temporary:
        temporary.write(text)
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)
