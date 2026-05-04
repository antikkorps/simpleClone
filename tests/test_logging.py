"""Tests pour le système de logs : resolve_log_dir, log_activity, errors logger."""
import json
import logging

from .conftest import flush_loggers


def test_resolve_log_dir_creates_directory(sc, isolated_logs):
    """Le dossier log/ doit être créé par la fixture isolée."""
    # isolated_logs setup déclenche déjà _setup_*_logger qui crée les sous-dossiers
    assert isolated_logs.exists()
    assert (isolated_logs / "errors").exists()
    assert (isolated_logs / "activity").exists()


def test_log_activity_writes_valid_json_line(sc, isolated_logs):
    """Chaque appel à log_activity produit une ligne JSON parseable."""
    sc.log_activity("copy", src="/a/b.txt", dest="/x/y.txt", size_bytes=1024)
    flush_loggers(sc)

    activity_file = isolated_logs / "activity" / "activity.log"
    assert activity_file.exists()
    lines = activity_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    entry = json.loads(lines[0])
    assert entry["op"] == "copy"
    assert entry["src"] == "/a/b.txt"
    assert entry["dest"] == "/x/y.txt"
    assert entry["size"] == 1024
    assert "ts" in entry


def test_log_activity_omits_optional_fields(sc, isolated_logs):
    """Quand src/dest/size sont None, les champs ne doivent pas apparaître."""
    sc.log_activity("custom_op")
    flush_loggers(sc)

    line = (isolated_logs / "activity" / "activity.log").read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    assert entry["op"] == "custom_op"
    assert "src" not in entry
    assert "dest" not in entry
    assert "size" not in entry


def test_log_activity_supports_extra_fields(sc, isolated_logs):
    """Les kwargs supplémentaires sont sérialisés dans le JSON."""
    sc.log_activity("copy", src="/a", dest="/b", custom_tag="autoclave_42")
    flush_loggers(sc)

    line = (isolated_logs / "activity" / "activity.log").read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    assert entry["custom_tag"] == "autoclave_42"


def test_log_activity_never_raises_even_on_logger_failure(sc, isolated_logs, monkeypatch):
    """log_activity doit avaler toute exception : la sync ne doit jamais être interrompue."""
    # On casse volontairement le logger
    def boom(*_a, **_kw):
        raise RuntimeError("logger broken")
    monkeypatch.setattr(sc._activity_logger, "info", boom)

    # Aucun raise ne doit remonter — sinon le test échoue avec l'exception
    sc.log_activity("copy", src="/a", dest="/b", size_bytes=1)


def test_error_logger_writes_to_errors_subdir(sc, isolated_logs):
    """Le logger d'erreurs écrit dans log/errors/, séparé de l'audit."""
    sc._error_logger.error("test error message")
    flush_loggers(sc)

    errors_file = isolated_logs / "errors" / "SimpleClone_Errors.log"
    assert errors_file.exists()
    content = errors_file.read_text(encoding="utf-8")
    assert "test error message" in content


def test_errors_log_is_isolated_from_activity(sc, isolated_logs):
    """Une erreur ne doit pas polluer le log d'activité (propagate=False)."""
    sc._error_logger.error("only in errors")
    sc.log_activity("copy", src="/a", dest="/b")
    flush_loggers(sc)

    activity_content = (isolated_logs / "activity" / "activity.log").read_text(encoding="utf-8")
    assert "only in errors" not in activity_content
    assert '"op": "copy"' in activity_content


def test_log_activity_handles_unicode_paths(sc, isolated_logs):
    """Les paths avec accents doivent être loggués correctement (UTF-8 + ensure_ascii=False)."""
    sc.log_activity("copy", src="/données/éléphant.txt", dest="/Z/élé.txt", size_bytes=10)
    flush_loggers(sc)

    line = (isolated_logs / "activity" / "activity.log").read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    assert entry["src"] == "/données/éléphant.txt"
    assert entry["dest"] == "/Z/élé.txt"


def test_lifecycle_events_have_expected_schema(sc, isolated_logs):
    """
    Schéma des entrées lifecycle (app_start, heartbeat, app_stop) :
    consommées par les outils de monitoring côté client, donc on les
    "fige" via un test pour éviter qu'une refacto silencieuse change le format.
    """
    sc.log_activity("app_start")
    sc.log_activity("heartbeat", state="running", uptime_s=900)
    sc.log_activity("app_stop", uptime_s=3600)
    flush_loggers(sc)

    entries = [
        json.loads(line)
        for line in (isolated_logs / "activity" / "activity.log")
        .read_text(encoding="utf-8").strip().splitlines()
    ]
    assert len(entries) == 3

    start, beat, stop = entries
    assert start["op"] == "app_start" and "ts" in start

    assert beat["op"] == "heartbeat"
    assert beat["state"] == "running"
    assert beat["uptime_s"] == 900

    assert stop["op"] == "app_stop"
    assert stop["uptime_s"] == 3600
