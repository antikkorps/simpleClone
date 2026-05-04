"""
Tests pour SyncHandler : copies, archivages, détection débranchement USB.

Le focus est sur les comportements à valeur de régression :
- Chaque opération réussie écrit la bonne entrée dans l'audit log.
- Quand la destination devient inaccessible, le callback on_dest_lost est
  appelé EXACTEMENT une fois (pas de spam, c'était le bug avant l'étape 4).
- _log_error sait court-circuiter quand la dest est partie pour ne pas
  écrire des centaines d'entrées "Disque non prêt" dans le fichier d'erreurs.
"""
import json
import shutil

from .conftest import flush_loggers


def _make_handler(sc, src, dst, callbacks, on_dest_lost=None):
    """Construit un SyncHandler avec les callbacks de test."""
    log_cb, status_cb, _, _ = callbacks
    return sc.SyncHandler(
        str(src), str(dst), log_cb, status_cb, on_dest_lost=on_dest_lost
    )


def test_copy_file_emits_audit_entry(sc, isolated_logs, src_dest_dirs, silent_callbacks):
    """Une copie réussie doit produire une ligne JSON op=copy avec la taille."""
    src, dst = src_dest_dirs
    src_file = src / "graph.csv"
    src_file.write_text("autoclave data" * 100)
    expected_size = src_file.stat().st_size

    handler = _make_handler(sc, src, dst, silent_callbacks)
    assert handler._copy_file(str(src_file)) is True
    assert (dst / "graph.csv").exists()

    flush_loggers(sc)
    line = (isolated_logs / "activity" / "activity.log").read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    assert entry["op"] == "copy"
    assert entry["src"].endswith("graph.csv")
    assert entry["dest"].endswith("graph.csv")
    assert entry["size"] == expected_size


def test_move_to_archive_emits_audit_entry(sc, isolated_logs, src_dest_dirs, silent_callbacks):
    """Un fichier supprimé de la source → archivé doit produire op=archive."""
    src, dst = src_dest_dirs
    src_file = src / "old.csv"
    src_file.write_text("data")

    handler = _make_handler(sc, src, dst, silent_callbacks)
    handler._copy_file(str(src_file))  # copie d'abord
    src_file.unlink()  # supprime de la source
    handler._move_to_archive(str(src_file))

    flush_loggers(sc)
    entries = [
        json.loads(line)
        for line in (isolated_logs / "activity" / "activity.log")
        .read_text(encoding="utf-8").strip().splitlines()
    ]
    archive_entries = [e for e in entries if e["op"] == "archive"]
    assert len(archive_entries) == 1
    assert "_Archive" in archive_entries[0]["dest"]
    assert archive_entries[0]["size"] > 0


def test_dest_lost_callback_fires_exactly_once(sc, isolated_logs, src_dest_dirs, silent_callbacks):
    """
    Si la dest est partie, on_dest_lost doit être appelé UNE fois sur N erreurs
    consécutives. C'est le mécanisme anti-spam qui évite des Mo de bruit dans
    le log d'erreurs en quelques minutes après un débranchement.
    """
    src, dst = src_dest_dirs
    shutil.rmtree(dst)  # dest inaccessible

    call_count = [0]
    def on_lost():
        call_count[0] += 1

    handler = _make_handler(sc, src, dst, silent_callbacks, on_dest_lost=on_lost)

    # Simule un flot d'erreurs (l'observer enchaînerait des _log_error en réel)
    for i in range(20):
        handler._log_error("OP", f"/file_{i}", Exception("Disque non prêt"))

    assert call_count[0] == 1, (
        f"on_dest_lost devrait être appelé 1 fois, pas {call_count[0]}. "
        "C'est ce qui empêche le spam d'erreurs en boucle."
    )


def test_dest_lost_does_not_fire_when_dest_accessible(sc, isolated_logs, src_dest_dirs, silent_callbacks):
    """Une erreur sur un fichier précis (pas la dest entière) ne doit pas trigger on_dest_lost."""
    src, dst = src_dest_dirs
    src_file = src / "f.txt"
    src_file.write_text("data")

    call_count = [0]
    def on_lost():
        call_count[0] += 1

    handler = _make_handler(sc, src, dst, silent_callbacks, on_dest_lost=on_lost)
    # Force une fausse erreur via _log_error directement
    handler._log_error("FAKE", "/some/path", Exception("permission denied"))

    # La dest est OK, donc on_dest_lost ne doit PAS être appelé
    assert call_count[0] == 0


def test_log_error_skips_file_logging_when_dest_lost(sc, isolated_logs, src_dest_dirs, silent_callbacks):
    """
    Quand la dest est partie, _log_error ne doit PAS écrire dans errors.log.
    C'est ce qui évite le fichier de plusieurs Mo en quelques minutes.
    """
    src, dst = src_dest_dirs
    shutil.rmtree(dst)  # Dest inaccessible

    handler = _make_handler(sc, src, dst, silent_callbacks, on_dest_lost=lambda: None)

    # 50 erreurs simulées
    for i in range(50):
        handler._log_error("OP", f"/file_{i}", Exception("Disque non prêt"))

    flush_loggers(sc)
    errors_file = isolated_logs / "errors" / "SimpleClone_Errors.log"
    # Le fichier peut ne pas exister, ou être vide
    if errors_file.exists():
        content = errors_file.read_text(encoding="utf-8")
        assert "Disque non prêt" not in content, (
            "Quand la dest est partie, _log_error ne doit RIEN écrire dans le log "
            "d'erreurs (sinon on a des Mo de bruit en quelques minutes)."
        )


def test_log_error_writes_to_errors_log_when_dest_ok(sc, isolated_logs, src_dest_dirs, silent_callbacks):
    """Une vraie erreur applicative (dest OK) doit bien atterrir dans errors.log."""
    src, dst = src_dest_dirs
    handler = _make_handler(sc, src, dst, silent_callbacks)
    handler._log_error("PERMISSION", "/locked/file.txt", Exception("Access denied"))

    flush_loggers(sc)
    errors_content = (isolated_logs / "errors" / "SimpleClone_Errors.log").read_text(encoding="utf-8")
    assert "PERMISSION" in errors_content
    assert "Access denied" in errors_content


def test_get_archive_path_includes_timestamp(sc, src_dest_dirs):
    """Le path d'archive doit avoir un timestamp pour éviter les collisions."""
    src, dst = src_dest_dirs
    handler = sc.SyncHandler(str(src), str(dst), lambda *a: None, lambda *a: None)

    src_file = src / "report.csv"
    src_file.write_text("x")
    archive = handler._get_archive_path(str(src_file))

    assert "_Archive" in str(archive)
    assert "report_" in archive.name
    assert archive.suffix == ".csv"
    # Format YYYYMMDD_HHMMSS attendu dans le nom
    parts = archive.stem.split("_")
    assert len(parts) >= 3, f"Pas de timestamp détecté dans {archive.name}"


def test_copy_failure_does_not_crash(sc, isolated_logs, src_dest_dirs, silent_callbacks):
    """Si la copie échoue (source absente), on retourne False sans lever."""
    src, dst = src_dest_dirs
    handler = _make_handler(sc, src, dst, silent_callbacks)

    # Le fichier source n'existe pas
    result = handler._copy_file(str(src / "nonexistent.txt"))
    assert result is False  # Doit gérer l'erreur, pas crasher
