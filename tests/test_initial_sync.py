"""
Tests pour initial_sync : copie complète, idempotence, archivage des orphelins.

Le test critique de cette suite est `test_archive_orphans_does_not_recurse_into_archive_folder` :
c'est la régression à protéger absolument. Une boucle infinie sur _Archive/
remplirait la dest jusqu'à saturation.
"""
import json

from .conftest import flush_loggers


def _populate(src):
    """Crée une arbo de test représentative dans src."""
    (src / "graph_001.csv").write_text("a")
    (src / "graph_002.csv").write_text("bb")
    sub = src / "2026-Q1"
    sub.mkdir()
    (sub / "cycle_42.txt").write_text("ccc")
    (sub / "cycle_43.txt").write_text("dddd")


def test_full_sync_copies_all_files(sc, isolated_logs, src_dest_dirs, silent_callbacks):
    """Une sync initiale doit copier toute la source vers la dest."""
    src, dst = src_dest_dirs
    _populate(src)

    log_cb, status_cb, progress_cb, _ = silent_callbacks
    handler = sc.SyncHandler(str(src), str(dst), log_cb, status_cb)

    total, errors = sc.initial_sync(str(src), str(dst), handler, progress_cb, log_cb)

    assert errors == 0
    assert total == 4
    assert (dst / "graph_001.csv").exists()
    assert (dst / "graph_002.csv").exists()
    assert (dst / "2026-Q1" / "cycle_42.txt").exists()
    assert (dst / "2026-Q1" / "cycle_43.txt").exists()


def test_sync_emits_audit_entries(sc, isolated_logs, src_dest_dirs, silent_callbacks):
    """Chaque fichier copié doit produire une ligne op=copy_initial."""
    src, dst = src_dest_dirs
    _populate(src)

    log_cb, status_cb, progress_cb, _ = silent_callbacks
    handler = sc.SyncHandler(str(src), str(dst), log_cb, status_cb)
    sc.initial_sync(str(src), str(dst), handler, progress_cb, log_cb)

    flush_loggers(sc)
    entries = [
        json.loads(line)
        for line in (isolated_logs / "activity" / "activity.log")
        .read_text(encoding="utf-8").strip().splitlines()
    ]
    initial_ops = [e for e in entries if e["op"] == "copy_initial"]
    assert len(initial_ops) == 4
    sizes = [e["size"] for e in initial_ops]
    assert all(s > 0 for s in sizes)


def test_idempotent_when_no_changes(sc, isolated_logs, src_dest_dirs, silent_callbacks):
    """Re-sync sans changement source ne doit pas re-copier (mtime déjà à jour)."""
    src, dst = src_dest_dirs
    _populate(src)

    log_cb, status_cb, progress_cb, _ = silent_callbacks
    handler = sc.SyncHandler(str(src), str(dst), log_cb, status_cb)
    sc.initial_sync(str(src), str(dst), handler, progress_cb, log_cb)
    flush_loggers(sc)

    # Compte les copies après la première sync
    activity_path = isolated_logs / "activity" / "activity.log"
    first_pass_lines = activity_path.read_text(encoding="utf-8").strip().splitlines()
    first_pass_count = len(first_pass_lines)

    # Re-sync
    sc.initial_sync(str(src), str(dst), handler, progress_cb, log_cb)
    flush_loggers(sc)
    second_pass_lines = activity_path.read_text(encoding="utf-8").strip().splitlines()

    # Le 2e passage ne doit avoir ajouté aucune ligne (rien à copier)
    assert len(second_pass_lines) == first_pass_count


def test_archive_orphans_moves_orphans_to_archive(sc, isolated_logs, src_dest_dirs, silent_callbacks):
    """Au retour de pause USB : les fichiers absents en source sont archivés."""
    src, dst = src_dest_dirs
    _populate(src)

    log_cb, status_cb, progress_cb, _ = silent_callbacks
    handler = sc.SyncHandler(str(src), str(dst), log_cb, status_cb)
    sc.initial_sync(str(src), str(dst), handler, progress_cb, log_cb)

    # Simule la suppression source pendant pause USB
    (src / "graph_001.csv").unlink()
    (src / "2026-Q1" / "cycle_42.txt").unlink()

    # Re-sync avec archivage des orphelins
    sc.initial_sync(str(src), str(dst), handler, progress_cb, log_cb, archive_orphans=True)

    # Les fichiers ne doivent plus être à leur emplacement d'origine
    assert not (dst / "graph_001.csv").exists()
    assert not (dst / "2026-Q1" / "cycle_42.txt").exists()
    # Mais doivent être dans _Archive/ (avec timestamp)
    archived = list((dst / "_Archive").rglob("graph_001_*.csv"))
    assert len(archived) == 1
    archived_sub = list((dst / "_Archive").rglob("cycle_42_*.txt"))
    assert len(archived_sub) == 1
    # Les fichiers non orphelins doivent rester intacts
    assert (dst / "graph_002.csv").exists()
    assert (dst / "2026-Q1" / "cycle_43.txt").exists()


def test_archive_orphans_does_not_recurse_into_archive_folder(sc, isolated_logs, src_dest_dirs, silent_callbacks):
    """
    RÉGRESSION CRITIQUE : le balayage des orphelins ne doit JAMAIS descendre
    dans _Archive/. Sinon les fichiers déjà archivés seraient ré-archivés à
    chaque passage = boucle infinie qui sature la destination.
    """
    src, dst = src_dest_dirs
    _populate(src)

    log_cb, status_cb, progress_cb, _ = silent_callbacks
    handler = sc.SyncHandler(str(src), str(dst), log_cb, status_cb)
    sc.initial_sync(str(src), str(dst), handler, progress_cb, log_cb)

    # Crée un orphelin et fait une première passe d'archivage
    (src / "graph_001.csv").unlink()
    sc.initial_sync(str(src), str(dst), handler, progress_cb, log_cb, archive_orphans=True)

    # Snapshot du contenu de _Archive après la 1ère passe
    archive_snapshot_1 = sorted(p.relative_to(dst) for p in (dst / "_Archive").rglob("*"))

    # Deuxième passe avec archive_orphans=True : ne doit RIEN faire de plus
    sc.initial_sync(str(src), str(dst), handler, progress_cb, log_cb, archive_orphans=True)
    archive_snapshot_2 = sorted(p.relative_to(dst) for p in (dst / "_Archive").rglob("*"))

    assert archive_snapshot_1 == archive_snapshot_2, (
        "_Archive/ a été modifié à la 2e passe. Le balayage récurse dans _Archive/, "
        "ce qui causerait une boucle infinie en production."
    )

    # Garde-fou : pas de _Archive imbriqué (signe le plus visible d'une récursion)
    assert not (dst / "_Archive" / "_Archive").exists(), (
        "Trouvé _Archive/_Archive : récursion détectée."
    )


def test_archive_orphans_emits_audit_entries(sc, isolated_logs, src_dest_dirs, silent_callbacks):
    """Chaque orphelin archivé doit produire une ligne op=archive_orphan."""
    src, dst = src_dest_dirs
    _populate(src)

    log_cb, status_cb, progress_cb, _ = silent_callbacks
    handler = sc.SyncHandler(str(src), str(dst), log_cb, status_cb)
    sc.initial_sync(str(src), str(dst), handler, progress_cb, log_cb)

    (src / "graph_001.csv").unlink()
    sc.initial_sync(str(src), str(dst), handler, progress_cb, log_cb, archive_orphans=True)

    flush_loggers(sc)
    entries = [
        json.loads(line)
        for line in (isolated_logs / "activity" / "activity.log")
        .read_text(encoding="utf-8").strip().splitlines()
    ]
    orphan_ops = [e for e in entries if e["op"] == "archive_orphan"]
    assert len(orphan_ops) == 1
    assert orphan_ops[0]["src"].endswith("graph_001.csv")
    assert "_Archive" in orphan_ops[0]["dest"]


def test_dest_newer_than_source_is_not_recopied(sc, isolated_logs, src_dest_dirs, silent_callbacks):
    """Si dest_mtime >= src_mtime, on saute la copie (optimisation initial_sync)."""
    src, dst = src_dest_dirs
    src_file = src / "stable.txt"
    src_file.write_text("v1")
    dst_file = dst / "stable.txt"
    dst_file.write_text("v1-pre-existing")
    # Force le mtime du dest à être plus récent que la source
    import os
    src_stat = src_file.stat()
    os.utime(str(dst_file), (src_stat.st_atime + 100, src_stat.st_mtime + 100))

    log_cb, status_cb, progress_cb, _ = silent_callbacks
    handler = sc.SyncHandler(str(src), str(dst), log_cb, status_cb)
    sc.initial_sync(str(src), str(dst), handler, progress_cb, log_cb)

    # Le contenu du dest n'a pas été écrasé par la source
    assert dst_file.read_text() == "v1-pre-existing"
