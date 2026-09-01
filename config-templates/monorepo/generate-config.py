"""
Generate a CircleCI continuation config for a monorepo.

Reads a modules.yml file from the consuming repo, determines which modules have
changed via git diff, and renders build-test-monorepo-continued.yml with one
build-test workflow per active module.

Per-module job overrides
------------------------
A module may declare, in modules.yml:

    overrides:
      test: my-custom-test        # a job name in .circleci/app-config.yml

The generator then emits `override-with: app-config/my-custom-test` for THAT
module's call site only. Modules that declare no override emit no
`override-with` key at all, so they compile against the central template's job.
Override names are validated against app-config.yml up front, because a bad
name would otherwise fall back silently to the central job.
"""

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import urllib.request

import yaml

# Per-module defaults applied before rendering, so a module can omit keys it
# doesn't need and the template never ends up with unresolved $MODULE_* markers.
MODULE_DEFAULTS = {
    "python-version": "3.12",
    "checkout-submodules": False,
    "build-image": True,
    "image-repository": "",
}

# Jobs in the template that expose an override point.
OVERRIDABLE_JOBS = ("test", "build")


def parse_args() -> argparse.Namespace:
    """Parse the CLI arguments passed by the generate-config step."""
    parser = argparse.ArgumentParser(description="Generate CircleCI monorepo config")
    parser.add_argument("--modules", required=True, help="Path to modules.yml in the consuming repo")
    parser.add_argument("--app-config", required=True, help="Path to app-config.yml in the consuming repo")
    parser.add_argument("--override-url", required=True, help="Raw URL of app-config.yml, pinned to this revision")
    parser.add_argument("--template-url", required=True, help="URL (or local path) to the continuation template")
    parser.add_argument("--config-commit", required=True, help="Central config commit or branch to reference")
    parser.add_argument("--base", required=True, help="Base git revision for diffing")
    parser.add_argument("--head", required=True, help="Head git revision (typically $CIRCLE_SHA1)")
    return parser.parse_args()


def has_changes(base: str, head: str, paths: list[str]) -> bool:
    """Return True if any of the given paths changed between base and head."""
    for path in paths:
        cmd = ["git", "diff", "--quiet", base, head, "--", path]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 128:
            raise RuntimeError(f"Git command failed: {' '.join(cmd)}")
        if result.returncode != 0:
            return True
    return False


def fetch_template(url: str) -> str:
    """Return the continuation template's contents from a URL or local path."""
    if not url.startswith("http"):
        with open(url) as f:
            return f.read()
    token = os.environ.get("GITHUB_TOKEN")
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"token {token}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def get_active_modules(modules_config: dict, base: str, head: str) -> list[dict]:
    """Return the modules to build: all on a global-path change, else those whose paths changed."""
    all_modules = list(modules_config["modules"])
    paths = modules_config.get("paths-for-all-modules", [".circleci"])

    if has_changes(base, head, paths):
        print(f"# Detected change in {paths} - including all modules", file=sys.stderr)
        return all_modules

    active_modules = []
    for module in all_modules:
        module_paths = [module["app-dir"]] + module.get("paths", [])
        if has_changes(base, head, module_paths):
            active_modules.append(module)
        else:
            print(f"# Skipping {module['app-dir']}: no changes detected", file=sys.stderr)
    return active_modules


def to_yaml_scalar(value) -> str:
    """Render a Python value as a CircleCI-safe YAML scalar."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def module_placeholder_key(key: str) -> str:
    """Map a modules.yml key to its template placeholder (python-version -> MODULE_PYTHON_VERSION)."""
    return "MODULE_" + key.upper().replace("-", "_")


def resolve_placeholder(s: str, placeholder: str, value: str) -> str:
    """Replace both $NAME and #NAME occurrences of a placeholder with the given value."""
    return s.replace(f"${placeholder}", value).replace(f"#{placeholder}", value)


def substring_between(s: str, start: str, end: str) -> str:
    """Return the text between the first start marker and the next end marker."""
    return s.split(start)[1].split(end)[0]


def strip_block(content: str, start: str, end: str) -> str:
    """Remove a start … end block and both sentinel lines in full."""
    lines, result, in_block = content.split("\n"), [], False
    for line in lines:
        if start in line:
            in_block = True
            continue
        if end in line:
            in_block = False
            continue
        if not in_block:
            result.append(line)
    return "\n".join(result)


def unwrap_block(content: str, start: str, end: str) -> str:
    """Keep the block body but drop the sentinel comment lines entirely."""
    for marker in (start, end):
        idx = content.find(marker)
        while idx != -1:
            line_start = content.rfind("\n", 0, idx) + 1
            line_end = content.find("\n", idx)
            line_end = line_end + 1 if line_end != -1 else len(content)
            content = content[:line_start] + content[line_end:]
            idx = content.find(marker)
    return content


def module_slug(module: dict) -> str:
    """A path is not a legal name: services/alpha -> services-alpha.

    Used for workflow names, job names and the orb job ref in override-with,
    all of which reject '/' (override-with reads it as an orb separator).
    """
    return module.get("slug") or re.sub(r"[^a-z0-9]+", "-", module["app-dir"].lower()).strip("-")


def validate_overrides(modules: list[dict], app_config_path: str) -> None:
    """Fail loudly on an override naming a job that app-config.yml does not define.

    override-with falls back to the central job when the target is missing, which
    is silent by design -- so a typo would otherwise produce a green build that
    quietly ignored the team's override.
    """
    declared: dict = {}
    if os.path.exists(app_config_path):
        with open(app_config_path) as f:
            declared = (yaml.safe_load(f) or {}).get("jobs") or {}
    for module in modules:
        for job, target in (module.get("overrides") or {}).items():
            if target not in declared:
                raise ValueError(
                    f"Module '{module['app-dir']}' declares overrides.{job}: '{target}', but "
                    f"{app_config_path} defines no job by that name. Available: {sorted(declared)}"
                )


def render_overrides(content: str, resolved_module: dict) -> str:
    """Emit override-with for the jobs this module overrides, and delete it for the rest.

    Resolved here rather than in the generic $MODULE_* loop below: resolve_placeholder
    is an unbounded str.replace, so a scalar key named 'override' would otherwise
    corrupt $MODULE_OVERRIDE_TEST into '<value>_TEST'.
    """
    overrides = resolved_module.pop("overrides", None) or {}
    unknown = set(overrides) - set(OVERRIDABLE_JOBS)
    if unknown:
        raise ValueError(
            f"Module '{resolved_module['app-dir']}' overrides unknown job(s): {sorted(unknown)}. "
            f"Overridable jobs are: {sorted(OVERRIDABLE_JOBS)}"
        )
    for job in OVERRIDABLE_JOBS:
        start, end = f"IF_OVERRIDE_{job.upper()}_START", f"IF_OVERRIDE_{job.upper()}_END"
        target = overrides.get(job)
        if target:
            content = unwrap_block(content, start, end)
            content = resolve_placeholder(content, f"MODULE_OVERRIDE_{job.upper()}", target)
        else:
            # Delete the line outright. An empty override-with value is a compile error.
            content = strip_block(content, start, end)
    return content


def render_module(module_template: str, module: dict) -> str:
    """Render one module's workflow block."""
    resolved_module = {**MODULE_DEFAULTS, **module}
    resolved_module["slug"] = module_slug(module)
    content = module_template

    if resolved_module["build-image"]:
        if not resolved_module["image-repository"]:
            raise ValueError(
                f"Module '{resolved_module['app-dir']}' has build-image: true but no "
                f"image-repository. Set image-repository, or set build-image: false."
            )
        content = unwrap_block(content, "IF_BUILD_IMAGE_START", "IF_BUILD_IMAGE_END")
    else:
        content = strip_block(content, "IF_BUILD_IMAGE_START", "IF_BUILD_IMAGE_END")

    content = render_overrides(content, resolved_module)

    for key, value in resolved_module.items():
        content = resolve_placeholder(content, module_placeholder_key(key), to_yaml_scalar(value))
    return content


def render_orb_block(content: str, any_override: bool, override_url: str) -> str:
    """Include the app-config orb only when some module actually overrides a job.

    The URL is written in literally rather than left as a pipeline parameter:
    config policies evaluate orb refs before parameter substitution, so a
    parameterised URL is invisible to any policy governing override orbs.
    """
    if any_override:
        content = unwrap_block(content, "IF_ANY_OVERRIDE_START", "IF_ANY_OVERRIDE_END")
        return resolve_placeholder(content, "OVERRIDE_ORB_URL", override_url)
    return strip_block(content, "IF_ANY_OVERRIDE_START", "IF_ANY_OVERRIDE_END")


def main() -> None:
    """Render and print the continuation config (or a no-op) for the changed modules."""
    args = parse_args()

    with open(args.modules) as f:
        modules_config = yaml.safe_load(f)

    active_modules = get_active_modules(modules_config, args.base, args.head)
    validate_overrides(active_modules, args.app_config)
    template_content = fetch_template(args.template_url)
    header = (
        "# Generated by generate-config.py - do not edit directly.\n"
        "# Source template: config-templates/monorepo/build-test-monorepo-continued.yml"
    )

    if not active_modules:
        parameters = substring_between(template_content, "#PARAMETERS_START", "#PARAMETERS_END").strip()
        print(f"""{header}
version: 2.1
{parameters}
workflows:
  no-modules-changed:
    jobs:
      - no-modules-changed
jobs:
  no-modules-changed:
    docker:
      - image: cimg/base:stable
    steps:
      - run: echo "No modules changed, nothing to do."
""")
        return

    any_override = any(module.get("overrides") for module in active_modules)

    module_template = substring_between(template_content, "#FOREACH_MODULE_START", "#FOREACH_MODULE_END")
    module_contents = [render_module(module_template, module) for module in active_modules]

    rendered = template_content.replace(module_template, "\n".join(module_contents))
    rendered = resolve_placeholder(rendered, "FOREACH_MODULE_START", "")
    rendered = resolve_placeholder(rendered, "FOREACH_MODULE_END", "")
    rendered = resolve_placeholder(rendered, "CONFIG_COMMIT", args.config_commit)
    rendered = render_orb_block(rendered, any_override, args.override_url)

    print(header)
    print(rendered)


if __name__ == "__main__":
    main()
