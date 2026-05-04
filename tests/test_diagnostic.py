"""
Tests pour la génération de diagnostic (bouton "?" → fichier export).

On teste les fonctions module-level (build_diagnostic_text, write_diagnostic_file,
_format_uptime, _read_last_n_lines, _find_last_activity_event), pas le dialog
tkinter qui les appelle. Les fonctions sont écrites pour être unitairement
testables — c'est l'intérêt de les avoir extraites de l'UI.
"""
import json
import time

from .conftest import flush_loggers


# -----------------------------------------------------------------------------
# _format_uptime
# -----------------------------------------------------------------------------

def test_format_uptime_zero(sc):
    assert sc._format_uptime(0) == "0 min"


def test_format_uptime_minutes_only(sc):
    assert sc._format_uptime(60 * 7) == "7 min"


def test_format_uptime_hours_and_minutes(sc):
    assert sc._format_uptime(3600 * 2 + 60 * 30) == "2 h 30 min"


def test_format_uptime_days(sc):
    assert sc._format_uptime(86400 * 3 + 3600 * 4 + 60 * 5) == "3 j 4 h 5 min"


def test_format_uptime_negative_clamps_to_zero(sc):
    """Tolérance : si l'horloge fait un saut arrière, on ne crashe pas."""
    assert sc._format_uptime(-100) == "0 min"


# -----------------------------------------------------------------------------
# _read_last_n_lines
# -----------------------------------------------------------------------------

def test_read_last_n_lines_returns_empty_for_missing_file(sc, tmp_path):
    assert sc._read_last_n_lines(tmp_path / "nope.log", 10) == []


def test_read_last_n_lines_returns_all_when_file_smaller(sc, tmp_path):
    f = tmp_path / "small.log"
    f.write_text("a\nb\nc\n")
    lines = sc._read_last_n_lines(f, 10)
    assert [line.strip() for line in lines] == ["a", "b", "c"]


def test_read_last_n_lines_truncates_to_last_n(sc, tmp_path):
    f = tmp_path / "big.log"
    f.write_text("\n".join(str(i) for i in range(100)) + "\n")
    lines = sc._read_last_n_lines(f, 5)
    assert [line.strip() for line in lines] == ["95", "96", "97", "98", "99"]


# -----------------------------------------------------------------------------
# _find_last_activity_event
# -----------------------------------------------------------------------------

def test_find_last_activity_event_returns_most_recent(sc, isolated_logs):
    """Le dernier event correspondant doit être retourné, en lisant en arrière."""
    sc.log_activity("copy", src="/a", dest="/b", size_bytes=1)
    sc.log_activity("rename", src="/c", dest="/d")
    sc.log_activity("copy", src="/x", dest="/y", size_bytes=2)
    sc.log_activity("heartbeat", state="running", uptime_s=42)
    flush_loggers(sc)

    ts, src = sc._find_last_activity_event(isolated_logs, {"copy", "copy_initial"})
    assert ts is not None
    assert src == "/x"


def test_find_last_activity_event_returns_none_when_no_match(sc, isolated_logs):
    sc.log_activity("heartbeat", state="running", uptime_s=42)
    flush_loggers(sc)

    ts, src = sc._find_last_activity_event(isolated_logs, {"copy"})
    assert ts is None
    assert src is None


# -----------------------------------------------------------------------------
# build_diagnostic_text
# -----------------------------------------------------------------------------

def test_build_diagnostic_includes_required_sections(sc, isolated_logs):
    """Le diagnostic doit contenir toutes les sections clés (régression de format)."""
    text = sc.build_diagnostic_text(
        state=sc.STATE_RUNNING,
        source_path="/data/source",
        dest_path="/media/usb",
        started_at=time.time() - 3600,
        log_dir=isolated_logs,
        config_data={"polling_interval": 2},
    )
    # En-tête + sections principales
    assert "SimpleClone — fichier de diagnostic" in text
    assert "Version app" in text
    assert sc.APP_VERSION in text
    assert "État de l'application" in text
    assert "Configuration" in text
    assert "Journal d'activité" in text
    assert "Journal d'erreurs" in text


def test_build_diagnostic_reflects_state(sc, isolated_logs):
    """Le libellé de l'état doit être traduit en français lisible."""
    text_running = sc.build_diagnostic_text(
        state=sc.STATE_RUNNING, source_path="/s", dest_path="/d",
        started_at=time.time(), log_dir=isolated_logs,
    )
    text_paused = sc.build_diagnostic_text(
        state=sc.STATE_PAUSED_USB, source_path="/s", dest_path="/d",
        started_at=time.time(), log_dir=isolated_logs,
    )
    assert "EN COURS" in text_running
    assert "EN PAUSE" in text_paused


def test_build_diagnostic_includes_config(sc, isolated_logs):
    """Les valeurs de config doivent apparaître dans la section dédiée."""
    text = sc.build_diagnostic_text(
        state=sc.STATE_STOPPED, source_path="", dest_path="",
        started_at=time.time(), log_dir=isolated_logs,
        config_data={"polling_interval": 5, "autostart_windows": True},
    )
    assert "polling_interval = 5" in text
    assert "autostart_windows = True" in text


def test_build_diagnostic_includes_recent_activity(sc, isolated_logs):
    """Les dernières lignes du log d'activité doivent être incluses."""
    sc.log_activity("copy", src="/foo/bar.csv", dest="/x/bar.csv", size_bytes=42)
    flush_loggers(sc)

    text = sc.build_diagnostic_text(
        state=sc.STATE_RUNNING, source_path="/s", dest_path="/d",
        started_at=time.time(), log_dir=isolated_logs,
    )
    assert "/foo/bar.csv" in text


def test_build_diagnostic_handles_empty_logs_gracefully(sc, isolated_logs):
    """Une app fraîchement installée n'a pas encore de log : pas de crash."""
    # On nettoie tout pour simuler le cas neuf
    for path in (isolated_logs / "activity").glob("*"):
        path.unlink()
    for path in (isolated_logs / "errors").glob("*"):
        path.unlink()

    text = sc.build_diagnostic_text(
        state=sc.STATE_STOPPED, source_path="", dest_path="",
        started_at=time.time(), log_dir=isolated_logs,
    )
    # Pas d'exception, et les sections "vide" sont signalées
    assert "vide ou inaccessible" in text


# -----------------------------------------------------------------------------
# write_diagnostic_file
# -----------------------------------------------------------------------------

def test_write_diagnostic_file_creates_file_in_diagnostic_dir(sc, isolated_logs):
    """Le fichier doit être créé dans log/diagnostic/ avec un nom horodaté."""
    path = sc.write_diagnostic_file("contenu de test", isolated_logs)

    assert path.exists()
    assert path.parent == isolated_logs / "diagnostic"
    assert path.name.startswith("simpleclone-diagnostic-")
    assert path.suffix == ".txt"
    assert path.read_text(encoding="utf-8") == "contenu de test"


def test_write_diagnostic_file_creates_dir_if_missing(sc, tmp_path):
    """L'opération doit être idempotente même sans dossier diagnostic préexistant."""
    log_dir = tmp_path / "fresh_log"
    # On ne crée PAS le dossier diagnostic à l'avance
    path = sc.write_diagnostic_file("x", log_dir)
    assert path.exists()
    assert path.parent.name == "diagnostic"
