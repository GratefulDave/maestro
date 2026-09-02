"""Atomic receipt/main publication for the artifact-factory slice.

Returns ``MAIN_PUBLICATION`` payloads and derived preflight refusals.
Never writes lane stage. Orchestrator is the only store composer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

from . import git_publication as gp
from . import scheduler_types as st
from . import workspace_receipt as wr
from .git_helper import GitError


@dataclass(frozen=True)
class PublicationPreflight:
    ok: bool
    code: str
    detail: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "ok": self.ok,
            "schema_version": st.CANONICAL_SCHEMA_VERSION,
        }


def preflight_publication(
    binding: gp.TargetBinding,
    *,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> PublicationPreflight:
    try:
        gp.revalidate_binding(binding)
    except gp.GitPublicationRefused as exc:
        return PublicationPreflight(False, exc.code, exc.detail)
    git = binding.git()
    try:
        if git.is_bare():
            return PublicationPreflight(
                False, "TARGET_BARE", binding.target_repository_root
            )
        try:
            head = git.symbolic_head()
        except GitError as exc:
            return PublicationPreflight(False, "TARGET_DETACHED", exc.detail)
        if head != binding.target_main_ref:
            return PublicationPreflight(
                False, "TARGET_HEAD_MISMATCH", f"{head}!={binding.target_main_ref}"
            )
        main_sha = git.rev_parse(binding.target_main_ref)
        if main_sha != expected_before_sha:
            return PublicationPreflight(
                False,
                "PUBLICATION_PREFLIGHT_REFUSED",
                f"main {main_sha} != expected-before",
            )
        if (
            not git.is_ancestor(expected_before_sha, reviewed_integration_sha)
            and expected_before_sha != reviewed_integration_sha
        ):
            return PublicationPreflight(
                False, "PUBLICATION_NOT_FAST_FORWARD", reviewed_integration_sha
            )
        if not git.diff_cached_quiet(
            expected_before_sha, git_dir=binding.target_worktree_git_dir
        ):
            return PublicationPreflight(
                False, "PUBLICATION_PREFLIGHT_REFUSED", "index dirty"
            )
        if not git.diff_files_quiet(git_dir=binding.target_worktree_git_dir):
            return PublicationPreflight(
                False, "PUBLICATION_PREFLIGHT_REFUSED", "worktree dirty"
            )
        touched = gp.publication_touched_paths(
            binding,
            expected_before_sha=expected_before_sha,
            reviewed_integration_sha=reviewed_integration_sha,
        )
        untracked = gp.collides(
            git.ls_others(ignored=False, git_dir=binding.target_worktree_git_dir),
            touched,
        )
        if untracked:
            return PublicationPreflight(
                False,
                "PUBLICATION_PREFLIGHT_REFUSED",
                "untracked:" + ",".join(untracked),
            )
        ignored = gp.collides(
            git.ls_others(ignored=True, git_dir=binding.target_worktree_git_dir),
            touched,
        )
        if ignored:
            return PublicationPreflight(
                False, "PUBLICATION_PREFLIGHT_REFUSED", "ignored:" + ",".join(ignored)
            )
    except GitError as exc:
        return PublicationPreflight(
            False, "PUBLICATION_PREFLIGHT_REFUSED", f"{exc.code}:{exc.detail}"
        )
    return PublicationPreflight(True, "OK")


def _open_journal(
    binding: gp.TargetBinding, run_id: str, fingerprint: str, *, create: bool
) -> wr.PublicationJournal:
    return wr.open_run_journal(
        binding.target_worktree_git_dir,
        run_id,
        fingerprint,
        expected_fingerprint=binding.target_sync_journal_fingerprint,
        create=create,
    )


def prepare_publication_journal(
    binding: gp.TargetBinding,
    *,
    run_id: str,
    review_input_fingerprint: str,
    final_review_artifact_id: str,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> wr.PublicationJournal:
    git = binding.git()
    receipt = gp.publication_receipt_payload(
        run_id=run_id,
        target_repository_fingerprint=binding.target_repository_fingerprint,
        target_main_ref=binding.target_main_ref,
        review_input_fingerprint=review_input_fingerprint,
        final_review_artifact_id=final_review_artifact_id,
        expected_before_sha=expected_before_sha,
        reviewed_integration_sha=reviewed_integration_sha,
    )
    owner = {
        "run_id": run_id,
        "schema_version": st.CANONICAL_SCHEMA_VERSION,
        "target_sync_journal_fingerprint": binding.target_sync_journal_fingerprint,
        "target_worktree_git_dir": binding.target_worktree_git_dir,
    }
    journal = _open_journal(binding, run_id, review_input_fingerprint, create=True)
    wr.write_owner_and_receipt(journal, owner, receipt)
    old_entries = wr.parse_ls_tree(git.ls_tree_z(expected_before_sha))
    new_entries = wr.parse_ls_tree(git.ls_tree_z(reviewed_integration_sha))
    leaves = wr.build_manifest(old_entries, new_entries)
    root_fd = wr.open_directory_nofollow(binding.target_repository_root)
    try:
        directories = wr.snapshot_directories(root_fd, [leaf.path for leaf in leaves])
    finally:
        os.close(root_fd)
    wr.write_manifest(journal, leaves, directories)
    wr.store_reviewed_blobs(journal, git, leaves)
    wr.prepare_reviewed_index(
        journal, git, reviewed_integration_sha, binding.target_worktree_git_dir
    )
    return journal


def cas_receipt_and_main(
    binding: gp.TargetBinding,
    journal: wr.PublicationJournal,
    *,
    run_id: str,
    review_input_fingerprint: str,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> tuple[str, str]:
    git = binding.git()
    receipt = wr.read_json_at(journal.run_fd, "receipt.json")
    receipt_bytes = st.canonical_bytes(receipt)
    receipt_oid = git.hash_object(receipt_bytes, object_type="blob", write=True)
    ref = gp.publication_ref_name(run_id, review_input_fingerprint)
    existing = git.read_ref(ref)
    if existing is not None:
        current = git.cat_file("blob", existing)
        if current != receipt_bytes:
            raise gp.GitPublicationRefused("PUBLICATION_RECEIPT_COLLISION", ref)
        if existing != receipt_oid:
            raise gp.GitPublicationRefused(
                "PUBLICATION_RECEIPT_COLLISION", f"{existing}!={receipt_oid}"
            )
        script = (
            f"start\n"
            f"update {binding.target_main_ref} {reviewed_integration_sha} {expected_before_sha}\n"
            f"commit\n"
        )
    else:
        script = (
            f"start\n"
            f"create {ref} {receipt_oid}\n"
            f"update {binding.target_main_ref} {reviewed_integration_sha} {expected_before_sha}\n"
            f"commit\n"
        )
    try:
        git.update_ref_stdin(script)
    except GitError as exc:
        raise gp.GitPublicationRefused(
            "PUBLICATION_REF_CAS_REFUSED", exc.detail
        ) from exc
    return ref, receipt_oid


def _verify_postconditions(
    binding: gp.TargetBinding,
    *,
    reviewed_integration_sha: str,
) -> None:
    gp.revalidate_binding(binding)
    git = binding.git()
    if git.symbolic_head() != binding.target_main_ref:
        raise gp.GitPublicationRefused("TARGET_HEAD_MISMATCH", git.symbolic_head())
    if git.rev_parse(binding.target_main_ref) != reviewed_integration_sha:
        raise gp.GitPublicationRefused("PUBLICATION_POSTCONDITION", "main != reviewed")
    if not git.diff_cached_quiet(
        reviewed_integration_sha, git_dir=binding.target_worktree_git_dir
    ):
        raise gp.GitPublicationRefused("PUBLICATION_POSTCONDITION", "index")
    if not git.diff_files_quiet(git_dir=binding.target_worktree_git_dir):
        raise gp.GitPublicationRefused("PUBLICATION_POSTCONDITION", "worktree")
    if git.ls_others(
        ignored=False, git_dir=binding.target_worktree_git_dir
    ) or git.ls_others(ignored=True, git_dir=binding.target_worktree_git_dir):
        raise gp.GitPublicationRefused("PUBLICATION_POSTCONDITION", "untracked")


def _install_reviewed_worktree(
    binding: gp.TargetBinding,
    journal: wr.PublicationJournal,
    *,
    reviewed_integration_sha: str,
) -> None:
    wr.create_index_lock(binding.target_worktree_git_dir, journal)
    wr.synchronize_publication_worktree(
        target_root=binding.target_repository_root,
        worktree_git_dir=binding.target_worktree_git_dir,
        journal=journal,
        object_format=binding.target_object_format,
        git=binding.git(),
    )
    _verify_postconditions(binding, reviewed_integration_sha=reviewed_integration_sha)


def publication_status(
    binding: gp.TargetBinding,
    *,
    run_id: str,
    review_input_fingerprint: str,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> Literal["unpublished", "in_progress", "published", "external_same_sha"]:
    gp.revalidate_binding(binding)
    git = binding.git()
    ref = gp.publication_ref_name(run_id, review_input_fingerprint)
    receipt_sha = git.read_ref(ref)
    main_sha = git.rev_parse(binding.target_main_ref)
    journal_published = False
    try:
        journal = _open_journal(binding, run_id, review_input_fingerprint, create=False)
        try:
            journal_published = wr.journal_is_published(journal)
        finally:
            wr.close_journal(journal)
    except wr.JournalError:
        journal_published = False
    if receipt_sha is not None and main_sha == reviewed_integration_sha:
        return "published"
    if main_sha == reviewed_integration_sha and receipt_sha is None:
        return "external_same_sha"
    if journal_published or receipt_sha is not None:
        return "in_progress"
    return "unpublished"


def reconcile_publication(
    binding: gp.TargetBinding,
    *,
    run_id: str,
    review_input_fingerprint: str,
    final_review_artifact_id: str,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> dict[str, Any] | PublicationPreflight:
    """Resume Git-before-ledger publication or report derived refusal.

    ``published`` / ``in_progress`` are for ``apply_amendment`` callers.
    Same main SHA without Maestro's receipt is never inferred as success.
    """
    status = publication_status(
        binding,
        run_id=run_id,
        review_input_fingerprint=review_input_fingerprint,
        expected_before_sha=expected_before_sha,
        reviewed_integration_sha=reviewed_integration_sha,
    )
    if status == "external_same_sha":
        return PublicationPreflight(
            False, "PUBLICATION_EXTERNAL_SAME_SHA", reviewed_integration_sha
        )
    if status == "published":
        git = binding.git()
        ref = gp.publication_ref_name(run_id, review_input_fingerprint)
        receipt_oid = git.read_ref(ref)
        if receipt_oid is None:
            raise gp.GitPublicationRefused("PUBLICATION_RECEIPT_MISSING", ref)
        journal = _open_journal(binding, run_id, review_input_fingerprint, create=True)
        try:
            _install_reviewed_worktree(
                binding, journal, reviewed_integration_sha=reviewed_integration_sha
            )
            return gp.main_publication_payload(
                review_input_fingerprint=review_input_fingerprint,
                receipt_ref=ref,
                receipt_object=receipt_oid,
                expected_before_sha=expected_before_sha,
                published_sha=reviewed_integration_sha,
            )
        finally:
            wr.close_journal(journal)
    if status == "in_progress":
        return _resume_in_progress(
            binding,
            run_id=run_id,
            review_input_fingerprint=review_input_fingerprint,
            final_review_artifact_id=final_review_artifact_id,
            expected_before_sha=expected_before_sha,
            reviewed_integration_sha=reviewed_integration_sha,
        )
    return PublicationPreflight(True, "unpublished")


def _resume_in_progress(
    binding: gp.TargetBinding,
    *,
    run_id: str,
    review_input_fingerprint: str,
    final_review_artifact_id: str,
    expected_before_sha: str,
    reviewed_integration_sha: str,
) -> dict[str, Any]:
    git = binding.git()
    journal = _open_journal(binding, run_id, review_input_fingerprint, create=True)
    try:
        if not wr.journal_is_published(journal):
            wr.close_journal(journal)
            journal = prepare_publication_journal(
                binding,
                run_id=run_id,
                review_input_fingerprint=review_input_fingerprint,
                final_review_artifact_id=final_review_artifact_id,
                expected_before_sha=expected_before_sha,
                reviewed_integration_sha=reviewed_integration_sha,
            )
            wr.publish_journal(journal)
        ref, receipt_oid = cas_receipt_and_main(
            binding,
            journal,
            run_id=run_id,
            review_input_fingerprint=review_input_fingerprint,
            expected_before_sha=expected_before_sha,
            reviewed_integration_sha=reviewed_integration_sha,
        )
        _install_reviewed_worktree(
            binding, journal, reviewed_integration_sha=reviewed_integration_sha
        )
        return gp.main_publication_payload(
            review_input_fingerprint=review_input_fingerprint,
            receipt_ref=ref,
            receipt_object=receipt_oid,
            expected_before_sha=expected_before_sha,
            published_sha=reviewed_integration_sha,
        )
    finally:
        wr.close_journal(journal)


def publish_reviewed_sha(
    binding: gp.TargetBinding,
    *,
    run_id: str,
    review_input_fingerprint: str,
    final_review_artifact_id: str,
    expected_before_sha: str,
    reviewed_integration_sha: str,
    hold_lock: bool = True,
) -> dict[str, Any] | PublicationPreflight:
    """Publish reviewed integration SHA to bound main exactly once.

    Crash windows are the named step functions; this composes them.
    """

    def _run() -> dict[str, Any] | PublicationPreflight:
        existing = reconcile_publication(
            binding,
            run_id=run_id,
            review_input_fingerprint=review_input_fingerprint,
            final_review_artifact_id=final_review_artifact_id,
            expected_before_sha=expected_before_sha,
            reviewed_integration_sha=reviewed_integration_sha,
        )
        if isinstance(existing, dict):
            return existing
        if existing.code == "PUBLICATION_EXTERNAL_SAME_SHA":
            return existing
        preflight = preflight_publication(
            binding,
            expected_before_sha=expected_before_sha,
            reviewed_integration_sha=reviewed_integration_sha,
        )
        if not preflight.ok:
            return preflight

        journal = prepare_publication_journal(
            binding,
            run_id=run_id,
            review_input_fingerprint=review_input_fingerprint,
            final_review_artifact_id=final_review_artifact_id,
            expected_before_sha=expected_before_sha,
            reviewed_integration_sha=reviewed_integration_sha,
        )
        try:
            wr.publish_journal(journal)
            try:
                ref, receipt_oid = cas_receipt_and_main(
                    binding,
                    journal,
                    run_id=run_id,
                    review_input_fingerprint=review_input_fingerprint,
                    expected_before_sha=expected_before_sha,
                    reviewed_integration_sha=reviewed_integration_sha,
                )
            except gp.GitPublicationRefused:
                wr.remove_zero_operation_published_journal(journal)
                raise
            _install_reviewed_worktree(
                binding, journal, reviewed_integration_sha=reviewed_integration_sha
            )
            return gp.main_publication_payload(
                review_input_fingerprint=review_input_fingerprint,
                receipt_ref=ref,
                receipt_object=receipt_oid,
                expected_before_sha=expected_before_sha,
                published_sha=reviewed_integration_sha,
            )
        finally:
            wr.close_journal(journal)

    if hold_lock:
        with gp.target_worktree_lock(binding.target_worktree_git_dir):
            return _run()
    return _run()


def cleanup_publication_journal(
    binding: gp.TargetBinding,
    *,
    run_id: str,
    review_input_fingerprint: str,
) -> None:
    journal = _open_journal(binding, run_id, review_input_fingerprint, create=False)
    try:
        wr.cleanup_success(journal)
    finally:
        wr.close_journal(journal)
