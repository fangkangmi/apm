"""Real CLI install/removal/prune contracts for manifestless skill packages."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshot, assert_unchanged
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [pytest.mark.integration, pytest.mark.lifecycle_smoke, pytest.mark.windows_compat]


@pytest.mark.parametrize("alias", [None, "azure-ai-alias"])
def test_install_remove_install_prune_skill(tmp_path: Path, alias: str | None) -> None:
    """Prune removes unlocked skill bytes while retaining a sibling and user files."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    repository = repositories.create("skills")
    skill_parent = ".github/plugins/azure-skills/skills"
    for name in ("azure-ai", "retained"):
        skill = repository.worktree / skill_parent / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Fixture skill\n---\n# {name}\n",
            encoding="utf-8",
        )
        (skill / "references").mkdir()
        (skill / "references" / "guide.md").write_text("Reference bytes\n", encoding="utf-8")
    commit = repositories.commit(repository, message="Add fixture skills")
    # Git transport is redirected to a real local repository; no downloader,
    # install, lockfile, integration, or prune code is mocked.
    remote = "https://gitlab.com/fixture/skills"
    environment = repositories.url_rewrite_subprocess_env(repository, remote)
    dependencies = [
        {"git": remote, "path": f"{skill_parent}/{name}", "ref": commit.sha}
        for name in ("azure-ai", "retained")
    ]
    if alias is not None:
        dependencies[0]["alias"] = alias
    project = LocalPackageFactory(isolated.work_root).create(
        "consumer", dependencies=dependencies, targets=("copilot",)
    )
    source_root = Path(__file__).resolve().parents[2] / "src"
    runner = ApmLifecycleRunner(
        (
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(source_root)!r}); "
            "from apm_cli.cli import main; main()",
        ),
        timeout_seconds=30,
    )

    def run(*args: str) -> str:
        result = runner.run(args, cwd=project.root, env=environment, scenario_id="prune-skill")
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout + result.stderr

    install_args = ("install", "--no-policy", "--parallel-downloads", "0")
    run(*install_args)
    lock_path = project.root / "apm.lock.yaml"
    lock = LockFile.read(lock_path)
    removed_key, removed = next(
        (key, dep)
        for key, dep in lock.dependencies.items()
        if dep.virtual_path.endswith("azure-ai")
    )
    modules = project.root / "apm_modules"
    removed_root = removed.to_dependency_ref().get_install_path(modules)
    retained = next(
        dep for dep in lock.dependencies.values() if dep.virtual_path.endswith("retained")
    )
    retained_root = retained.to_dependency_ref().get_install_path(modules)
    assert (removed_root / "SKILL.md").is_file()
    assert not (removed_root / "apm.yml").exists()
    deployed = project.root / ".agents" / "skills"
    removed_deployment = deployed / (alias or "azure-ai")
    assert (removed_deployment / "SKILL.md").is_file()
    retained_before = ArtifactSnapshot.capture(retained_root)
    deployed_before = ArtifactSnapshot.capture(deployed / "retained")

    manifest = load_yaml(project.manifest_path)
    manifest["dependencies"]["apm"] = dependencies[1:]
    dump_yaml(manifest, project.manifest_path)
    run(*install_args)
    assert removed_key not in LockFile.read(lock_path).dependencies
    assert not removed_deployment.exists()
    assert removed_root.exists(), "Install leaves orphan source bytes for prune"
    sentinel = modules / "user-notes.txt"
    sentinel.write_text("Keep these notes\n", encoding="utf-8")
    before_dry_run = ArtifactSnapshot.capture(project.root)

    preview = run("prune", "--dry-run")
    assert "1 orphaned package(s)" in preview
    assert_unchanged(before_dry_run, ArtifactSnapshot.capture(project.root))
    assert "Pruned 1 orphaned package(s)" in run("prune")
    assert not removed_root.exists()
    assert_unchanged(retained_before, ArtifactSnapshot.capture(retained_root))
    assert_unchanged(deployed_before, ArtifactSnapshot.capture(deployed / "retained"))
    assert sentinel.read_text(encoding="utf-8") == "Keep these notes\n"
    before_repeat = ArtifactSnapshot.capture(project.root)
    assert "No orphaned packages" in run("prune")
    assert_unchanged(before_repeat, ArtifactSnapshot.capture(project.root))


@pytest.mark.parametrize("alias", [None, "bundle-alias"])
def test_prune_preserves_declared_skill_bundle(tmp_path: Path, alias: str | None) -> None:
    """Real manifestless bundles survive prune and produce no compile orphan warning."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "isolated", base_env=os.environ)
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    repository = repositories.create("bundle")
    for name in ("alpha", "beta"):
        skill = repository.worktree / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Bundle fixture skill\n---\n# {name}\n",
            encoding="utf-8",
        )
    commit = repositories.commit(repository, message="Add manifestless skill bundle")
    remote = "https://gitlab.com/fixture/bundle"
    environment = repositories.url_rewrite_subprocess_env(repository, remote)
    dependency = {"git": remote, "ref": commit.sha}
    if alias is not None:
        dependency["alias"] = alias
    project = LocalPackageFactory(isolated.work_root).create(
        "consumer", dependencies=(dependency,), targets=("copilot",)
    )
    source_root = Path(__file__).resolve().parents[2] / "src"
    runner = ApmLifecycleRunner(
        (
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {str(source_root)!r}); "
            "from apm_cli.cli import main; main()",
        ),
        timeout_seconds=30,
    )

    def run(*args: str) -> str:
        result = runner.run(args, cwd=project.root, env=environment, scenario_id="prune-bundle")
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout + result.stderr

    run("install", "--no-policy", "--parallel-downloads", "0")
    modules = project.root / "apm_modules"
    bundle = modules / (alias or "fixture/bundle")
    assert not (bundle / "apm.yml").exists()
    assert not (bundle / "SKILL.md").exists()
    assert not (bundle / ".apm").exists()
    for name in ("alpha", "beta"):
        assert (bundle / "skills" / name / "SKILL.md").is_file()
        assert (project.root / ".agents" / "skills" / name / "SKILL.md").is_file()
    before = ArtifactSnapshot.capture(project.root)
    for args in (("prune", "--dry-run"), ("prune",)):
        assert "No orphaned packages" in run(*args)
        assert_unchanged(before, ArtifactSnapshot.capture(project.root))
    output = run("compile")
    assert "orphaned package(s)" not in output
    assert "Run 'apm prune'" not in output
    for name in ("alpha", "beta"):
        assert (bundle / "skills" / name / "SKILL.md").is_file()
