"""
Fixtures globales pytest pour la suite de tests SimpleClone.

Choix d'archi :
- On stub tkinter et watchdog AVANT le premier import de SimpleClone (autouse
  scope=session) pour pouvoir tester la logique sans environnement graphique.
- Chaque test qui touche aux loggers utilise la fixture `isolated_logs` :
  les handlers sont reconfigurés vers un répertoire tmp dédié au test, ce
  qui garantit l'isolation et empêche la pollution du repo / d'autres tests.
"""
import importlib.util
import logging
import sys
from pathlib import Path

import pytest


# -----------------------------------------------------------------------------
# Stubs des modules graphiques / système
# -----------------------------------------------------------------------------

class _Stub:
    """Stub générique : tout attribut / appel renvoie un autre stub."""

    def __getattr__(self, _name):
        return _Stub()

    def __call__(self, *_args, **_kwargs):
        return _Stub()


def _install_stubs():
    """Installe les stubs tkinter + watchdog dans sys.modules."""
    for name in (
        "tkinter",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.scrolledtext",
        "tkinter.ttk",
    ):
        sys.modules.setdefault(name, _Stub())

    if "watchdog" not in sys.modules:
        watchdog = type(sys)("watchdog")
        watchdog_observers = type(sys)("watchdog.observers")
        watchdog_observers_polling = type(sys)("watchdog.observers.polling")
        watchdog_events = type(sys)("watchdog.events")

        class _PollingObserver:
            def __init__(self, **_kw):
                pass

            def schedule(self, *_a, **_kw):
                pass

            def start(self):
                pass

            def stop(self):
                pass

            def join(self, **_kw):
                pass

        class _FileSystemEventHandler:
            def __init__(self):
                pass

        watchdog_observers_polling.PollingObserver = _PollingObserver
        watchdog_events.FileSystemEventHandler = _FileSystemEventHandler
        sys.modules["watchdog"] = watchdog
        sys.modules["watchdog.observers"] = watchdog_observers
        sys.modules["watchdog.observers.polling"] = watchdog_observers_polling
        sys.modules["watchdog.events"] = watchdog_events


@pytest.fixture(scope="session", autouse=True)
def _stub_modules():
    """Stubbing actif pendant toute la session de test."""
    _install_stubs()
    yield


# -----------------------------------------------------------------------------
# Import dynamique de SimpleClone.py (pas un package)
# -----------------------------------------------------------------------------

@pytest.fixture(scope="session")
def sc():
    """Charge le module SimpleClone une seule fois pour la session."""
    if "SimpleClone" in sys.modules:
        return sys.modules["SimpleClone"]
    project_root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "SimpleClone", project_root / "SimpleClone.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["SimpleClone"] = module
    return module


# -----------------------------------------------------------------------------
# Isolation du système de logs
# -----------------------------------------------------------------------------

def _reset_logger(logger_name):
    """Détache et ferme tous les handlers d'un logger."""
    logger = logging.getLogger(logger_name)
    for handler in list(logger.handlers):
        try:
            handler.close()
        except Exception:
            pass
        logger.removeHandler(handler)


@pytest.fixture
def isolated_logs(tmp_path, monkeypatch, sc):
    """
    Force le dossier de logs vers un tmp_path dédié au test, et reconfigure
    les loggers pour qu'ils écrivent réellement dedans.
    Le yield retourne le path racine `log/` du test.
    """
    log_root = tmp_path / "log"

    # Patch du cache module-level + override de _resolve_log_dir pour la durée du test
    monkeypatch.setattr(sc, "_resolved_log_dir", log_root)

    # Reconfigure les handlers vers le nouveau path
    _reset_logger("simpleclone.errors")
    _reset_logger("simpleclone.activity")
    sc._setup_error_logger()
    sc._setup_activity_logger()

    yield log_root

    # Cleanup : on relâche les file handles avant que tmp_path soit supprimé
    _reset_logger("simpleclone.errors")
    _reset_logger("simpleclone.activity")


# -----------------------------------------------------------------------------
# Helpers communs aux tests sync
# -----------------------------------------------------------------------------

@pytest.fixture
def src_dest_dirs(tmp_path):
    """Crée deux dossiers vides src/ et dst/ pour les tests de synchro."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    return src, dst


@pytest.fixture
def silent_callbacks():
    """
    Triplet de callbacks (log, status, progress) qui n'affichent rien mais
    capturent les messages de log dans une liste, accessible via le 4e élément.
    """
    captured = []

    def log_cb(msg, tag):
        captured.append((tag, msg))

    def status_cb(_msg):
        pass

    def progress_cb(_current, _total):
        pass

    return log_cb, status_cb, progress_cb, captured


def flush_loggers(sc):
    """Force le flush des handlers de fichier — à appeler avant de lire un log."""
    for name in ("simpleclone.errors", "simpleclone.activity"):
        for handler in logging.getLogger(name).handlers:
            try:
                handler.flush()
            except Exception:
                pass
