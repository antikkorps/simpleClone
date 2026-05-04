# -*- coding: utf-8 -*-
"""
SimpleClone - Logique métier (sans dépendance UI)

Ce module regroupe tout ce qui n'a pas besoin de tkinter / pystray :
configuration, logging (errors + audit), démarrage automatique Windows,
moteur de synchronisation (SyncHandler + initial_sync), et helpers de
diagnostic. C'est l'ensemble du code testable unitairement, sans X server.

L'UI (SimpleCloneApp, system tray, génération d'icône) vit dans simpleclone_ui.py.
"""

import os
import sys
import json
import shutil
import time
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path

# Watchdog : bibliothèque de surveillance de fichiers
# Installation : pip install watchdog
from watchdog.events import FileSystemEventHandler


# =============================================================================
# CONFIGURATION
# =============================================================================

APP_VERSION = "0.11.0"  # À mettre à jour à chaque release (utilisé dans le diagnostic)

ARCHIVE_FOLDER_NAME = "_Archive"  # Dossier où sont déplacés les fichiers supprimés

# Structure des logs (à côté de l'exe ou dans %APPDATA% en fallback) :
#   log/
#   ├── errors/      → SimpleClone_Errors.log (rotation par taille)
#   ├── activity/    → activity.log + activity.log.YYYY-MM-DD (rotation quotidienne, 6 ans)
#   └── diagnostic/  → simpleclone-diagnostic-YYYYMMDD-HHMMSS.txt (export user)
LOG_DIR_NAME = "log"
ERRORS_DIR_NAME = "errors"
ACTIVITY_DIR_NAME = "activity"
DIAGNOSTIC_DIR_NAME = "diagnostic"
ERRORS_LOG_FILE_NAME = "SimpleClone_Errors.log"
ACTIVITY_LOG_FILE_NAME = "activity.log"

# Rétention du log d'activité : politique client = 5 ans pour les graphiques
# d'autoclaves, on garde 6 ans de logs pour avoir une marge de sécurité.
ACTIVITY_LOG_RETENTION_DAYS = 2190  # 6 × 365

LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 Mo par fichier de log d'erreurs avant rotation
LOG_BACKUP_COUNT = 3              # Nombre d'anciennes versions d'erreurs conservées

DEFAULT_POLLING_INTERVAL = 2  # Intervalle de vérification en secondes (polling)
DEST_RETRY_INTERVAL = 5  # Délai entre deux tentatives de reconnexion de la destination
MAX_LOG_LINES_UI = 1000  # Limite de lignes dans le journal de l'UI (évite la fuite mémoire en H24)
LOG_TRIM_BATCH = 100     # Nombre de lignes supprimées d'un coup quand la limite est atteinte

# Heartbeat : preuve périodique que l'app est vivante (cas H24 sur site client).
# 15 minutes est un bon compromis : assez fréquent pour détecter une panne dans
# l'heure, pas trop pour éviter d'inonder le log d'activité.
HEARTBEAT_INTERVAL_S = 15 * 60

# États de la surveillance
STATE_STOPPED = "stopped"
STATE_RUNNING = "running"
STATE_PAUSED_USB = "paused_usb"  # Destination inaccessible (clé débranchée)


# =============================================================================
# LOGGERS (erreurs avec rotation par taille + audit d'activité avec rotation quotidienne)
# =============================================================================

# Cache du dossier log/ résolu (évite de retester l'écriture à chaque appel)
_resolved_log_dir = None


def _resolve_log_dir():
    """
    Détermine le dossier `log/` à utiliser. Stratégie :
    1. À côté de l'exe (ou du script) — discoverable, reste avec l'app
    2. Fallback %APPDATA%\\SimpleClone\\log si pas de droits écriture (typique
       quand l'exe est dans Program Files)
    3. Fallback ~/.config/SimpleClone/log (Linux/dev)
    On teste réellement l'écriture car mkdir réussit parfois là où la
    création de fichier échoue (ACL Windows tordues).
    """
    global _resolved_log_dir
    if _resolved_log_dir is not None:
        return _resolved_log_dir

    candidates = []
    exe_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    candidates.append(exe_dir / LOG_DIR_NAME)

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "SimpleClone" / LOG_DIR_NAME)
    else:
        candidates.append(Path.home() / ".config" / "SimpleClone" / LOG_DIR_NAME)

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_test"
            probe.touch()
            probe.unlink()
            _resolved_log_dir = candidate
            return _resolved_log_dir
        except Exception:
            continue

    # Dernier recours : tmp (les logs ne survivront pas au reboot mais l'app tourne)
    import tempfile
    _resolved_log_dir = Path(tempfile.gettempdir()) / "SimpleClone-log"
    _resolved_log_dir.mkdir(parents=True, exist_ok=True)
    return _resolved_log_dir


def _setup_error_logger():
    """
    Logger d'erreurs : rotation par TAILLE (5 Mo × 3) — ce log est pour le
    debug technique, pas pour l'audit. On veut éviter qu'il grossisse sans
    limite, peu importe la rétention.
    """
    logger = logging.getLogger("simpleclone.errors")
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    if not logger.handlers:
        try:
            errors_dir = _resolve_log_dir() / ERRORS_DIR_NAME
            errors_dir.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                errors_dir / ERRORS_LOG_FILE_NAME,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter(
                "[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            ))
            logger.addHandler(handler)
        except Exception:
            logger.addHandler(logging.NullHandler())
    return logger


def _setup_activity_logger():
    """
    Logger d'activité (audit légal) : rotation quotidienne via
    TimedRotatingFileHandler. Le fichier courant est `activity.log` ;
    après rotation à minuit il devient `activity.log.YYYY-MM-DD`.
    Conservation : ACTIVITY_LOG_RETENTION_DAYS jours, le handler supprime
    automatiquement les fichiers plus anciens.
    """
    logger = logging.getLogger("simpleclone.activity")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        try:
            activity_dir = _resolve_log_dir() / ACTIVITY_DIR_NAME
            activity_dir.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.TimedRotatingFileHandler(
                activity_dir / ACTIVITY_LOG_FILE_NAME,
                when="midnight",
                interval=1,
                backupCount=ACTIVITY_LOG_RETENTION_DAYS,
                encoding="utf-8",
                utc=False,
            )
            # Pas de préfixe ajouté par le logger : chaque ligne est déjà du JSON
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
        except Exception:
            logger.addHandler(logging.NullHandler())
    return logger


_error_logger = _setup_error_logger()
_activity_logger = _setup_activity_logger()


def log_activity(operation, src=None, dest=None, size_bytes=None, **extra):
    """
    Écrit une ligne JSON dans le log d'activité (audit).
    Format : {"ts": ISO, "op": str, "src": str, "dest": str, "size": int, ...}
    Ne lève jamais d'exception — un échec de log ne doit pas interrompre la sync.
    """
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "op": operation,
    }
    if src is not None:
        entry["src"] = str(src)
    if dest is not None:
        entry["dest"] = str(dest)
    if size_bytes is not None:
        entry["size"] = size_bytes
    if extra:
        entry.update(extra)
    try:
        _activity_logger.info(json.dumps(entry, ensure_ascii=False))
    except Exception:
        pass  # Logger ou JSON cassé : on ignore, jamais bloquer la sync


# =============================================================================
# GESTION DE LA CONFIGURATION UTILISATEUR (JSON dans %APPDATA%)
# =============================================================================

class ConfigManager:
    """
    Charge et sauvegarde la config persistante de l'utilisateur.
    Sur Windows : %APPDATA%\\SimpleClone\\config.json
    Sur Linux/Mac (dev) : ~/.config/SimpleClone/config.json
    Aucune erreur de lecture/écriture ne doit faire crasher l'app : si la config
    est absente ou corrompue, on retombe sur les valeurs par défaut.
    """

    DEFAULTS = {
        "source_path": "",
        "dest_path": "",
        "autostart_windows": False,
        "autostart_surveillance": False,
        "polling_interval": DEFAULT_POLLING_INTERVAL,
        "start_minimized": False,
    }

    def __init__(self):
        self.config_dir = self._get_config_dir()
        self.config_file = self.config_dir / "config.json"
        self.data = dict(self.DEFAULTS)

    @staticmethod
    def _get_config_dir():
        if sys.platform == "win32":
            base = os.environ.get("APPDATA")
            if base:
                return Path(base) / "SimpleClone"
        # Fallback Linux/Mac (utile pour dev en WSL)
        return Path.home() / ".config" / "SimpleClone"

    def load(self):
        """Charge la config depuis le disque. Silencieux en cas d'erreur."""
        try:
            if self.config_file.exists():
                with open(self.config_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # Merge avec defaults : les clés inconnues sont ignorées,
                # les clés manquantes prennent leur valeur par défaut.
                for key in self.DEFAULTS:
                    if key in loaded:
                        self.data[key] = loaded[key]
        except Exception:
            pass  # Config corrompue ou illisible : on garde les defaults
        return self.data

    def save(self):
        """Sauvegarde atomique (write-then-rename) pour éviter la corruption."""
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.config_file.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
            tmp.replace(self.config_file)
        except Exception:
            pass  # Échec d'écriture : pas critique, on retentera au prochain change

    def set(self, key, value):
        """Met à jour une clé et sauvegarde immédiatement."""
        if key in self.DEFAULTS:
            self.data[key] = value
            self.save()

    def get(self, key):
        return self.data.get(key, self.DEFAULTS.get(key))


# =============================================================================
# DÉMARRAGE AUTOMATIQUE AVEC WINDOWS (registre HKCU\...\Run)
# =============================================================================
#
# On utilise HKCU plutôt que HKLM (pas besoin de droits admin) et le registre
# plutôt que le dossier Startup (plus propre, plus discret).
# Sur les autres OS, ces fonctions sont des no-op : permet de tester l'UI sous
# WSL/Linux sans crash.

AUTOSTART_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_REG_NAME = "SimpleClone"


def _get_autostart_command():
    """
    Construit la commande à enregistrer dans le registre.
    En mode frozen (PyInstaller) : on lance directement l'exe.
    En mode dev : on lance pythonw.exe + le script (sans console noire).
    Le flag --minimized indique à l'app de démarrer cachée dans la zone de
    notification.
    Note : en mode dev, on enregistre le chemin du module SimpleClone.py
    (entry point), pas du module core, car c'est lui qui lance l'app.
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    # Entry point : SimpleClone.py au même niveau que ce module
    script = Path(__file__).resolve().parent / "SimpleClone.py"
    # En dev, on tente pythonw.exe (pas de console) sinon on retombe sur python
    exe = sys.executable
    if sys.platform == "win32":
        candidate = Path(exe).with_name("pythonw.exe")
        if candidate.exists():
            exe = str(candidate)
    return f'"{exe}" "{script}" --minimized'


def set_windows_autostart(enabled):
    """Active/désactive le démarrage avec Windows. Retourne True si succès."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, AUTOSTART_REG_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key, AUTOSTART_REG_NAME, 0, winreg.REG_SZ, _get_autostart_command()
                )
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_REG_NAME)
                except FileNotFoundError:
                    pass  # Clé déjà absente, idempotent
        return True
    except Exception:
        return False


def is_windows_autostart_enabled():
    """Vérifie si la clé est présente dans le registre (source de vérité)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, AUTOSTART_REG_KEY, 0, winreg.KEY_READ
        ) as key:
            winreg.QueryValueEx(key, AUTOSTART_REG_NAME)
        return True
    except Exception:
        return False


# =============================================================================
# GESTIONNAIRE D'ÉVÉNEMENTS FICHIERS
# =============================================================================

class SyncHandler(FileSystemEventHandler):
    """
    Gère les événements du système de fichiers détectés par watchdog.
    Réplique chaque changement de la source vers la destination.
    """

    def __init__(self, source_path, dest_path, log_callback, status_callback,
                 on_dest_lost=None):
        super().__init__()
        self.source_path = Path(source_path)
        self.dest_path = Path(dest_path)
        self.archive_path = self.dest_path / ARCHIVE_FOLDER_NAME
        self.log_callback = log_callback  # Fonction pour afficher les logs dans l'interface
        self.status_callback = status_callback  # Fonction pour mettre à jour le statut
        # Callback appelé quand on détecte que la destination n'est plus accessible
        # (clé USB débranchée). L'app bascule alors en mode PAUSED_USB.
        self.on_dest_lost = on_dest_lost
        self._dest_lost_notified = False  # Évite le spam quand la dest est partie
        self.error_count = 0

    def _is_dest_accessible(self):
        """Test rapide d'accessibilité de la destination racine."""
        try:
            return self.dest_path.is_dir()
        except OSError:
            return False  # Drive débranché : .is_dir() peut lever sur Windows

    def _get_dest_path(self, src_path):
        """Calcule le chemin de destination équivalent pour un chemin source."""
        relative_path = Path(src_path).relative_to(self.source_path)
        return self.dest_path / relative_path

    def _get_archive_path(self, src_path):
        """Calcule le chemin d'archive pour un fichier supprimé."""
        relative_path = Path(src_path).relative_to(self.source_path)
        # Ajoute un timestamp pour éviter les conflits de noms
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_name = f"{relative_path.stem}_{timestamp}{relative_path.suffix}"
        return self.archive_path / relative_path.parent / archive_name

    def _log_error(self, operation, path, error):
        """
        Enregistre une erreur dans le fichier log ET continue l'exécution.
        C'est crucial : le script ne doit JAMAIS s'arrêter sur une erreur.
        Si la destination n'est plus accessible (clé USB débranchée), on déclenche
        une bascule en pause au lieu de spammer le log à chaque opération qui rate.
        """
        # Détection débranchement USB : si la dest racine n'existe plus, on
        # considère qu'on est dans ce cas. On notifie l'app UNE SEULE fois et
        # on n'écrit pas l'erreur dans le log (évite SimpleClone_Errors.log
        # qui grossit de plusieurs Mo en quelques minutes).
        if not self._is_dest_accessible():
            if not self._dest_lost_notified:
                self._dest_lost_notified = True
                self.log_callback(
                    "⚠ Destination inaccessible — mise en pause automatique",
                    "warning"
                )
                if self.on_dest_lost:
                    self.on_dest_lost()
            return  # On n'écrit rien dans le log : ce serait du bruit

        self.error_count += 1
        # Logging avec rotation automatique (RotatingFileHandler).
        # logger.error() n'élève jamais d'exception → safe en H24.
        _error_logger.error(f"{operation} ERREUR: {path}\n    -> {error}")

        # Affichage dans l'interface
        self.log_callback(f"❌ ERREUR: {Path(path).name} - {str(error)[:50]}", "error")

    def _log_success(self, operation, path):
        """Affiche un message de succès dans l'interface."""
        self.log_callback(f"✓ {operation}: {Path(path).name}", "success")

    def _copy_file(self, src_path):
        """
        Copie un fichier de la source vers la destination.
        Utilise shutil.copy2 pour préserver les métadonnées (dates, permissions).
        """
        try:
            dest_path = self._get_dest_path(src_path)
            # Crée le dossier parent si nécessaire
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dest_path)
            # Audit log : prouve que la copie a eu lieu (pour graphiques d'autoclaves)
            try:
                size = dest_path.stat().st_size
            except OSError:
                size = None
            log_activity("copy", src=src_path, dest=dest_path, size_bytes=size)
            self._log_success("Copié", src_path)
            return True
        except Exception as e:
            # GESTION D'ERREUR : on log et on continue, JAMAIS de crash
            self._log_error("COPIE", src_path, e)
            return False

    def _move_to_archive(self, src_path):
        """
        Déplace un fichier vers le dossier d'archive au lieu de le supprimer.
        Permet de récupérer les fichiers supprimés par erreur.
        """
        try:
            dest_file = self._get_dest_path(src_path)
            if dest_file.exists():
                # Capture la taille avant le move (le fichier source disparaît après)
                try:
                    size = dest_file.stat().st_size
                except OSError:
                    size = None
                archive_file = self._get_archive_path(src_path)
                archive_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dest_file), str(archive_file))
                log_activity("archive", src=src_path, dest=archive_file, size_bytes=size)
                self._log_success("Archivé", src_path)
                return True
        except Exception as e:
            self._log_error("ARCHIVAGE", src_path, e)
            return False

    def _create_directory(self, src_path):
        """Crée un dossier dans la destination."""
        try:
            dest_path = self._get_dest_path(src_path)
            dest_path.mkdir(parents=True, exist_ok=True)
            self._log_success("Dossier créé", src_path)
            return True
        except Exception as e:
            self._log_error("CRÉATION DOSSIER", src_path, e)
            return False

    def _remove_directory(self, src_path):
        """Archive le contenu d'un dossier supprimé."""
        try:
            dest_path = self._get_dest_path(src_path)
            if dest_path.exists():
                # Déplace le dossier vers l'archive
                archive_path = self._get_archive_path(src_path)
                archive_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dest_path), str(archive_path))
                log_activity("archive_dir", src=src_path, dest=archive_path)
                self._log_success("Dossier archivé", src_path)
            return True
        except Exception as e:
            self._log_error("ARCHIVAGE DOSSIER", src_path, e)
            return False

    # -------------------------------------------------------------------------
    # ÉVÉNEMENTS WATCHDOG
    # -------------------------------------------------------------------------

    def on_created(self, event):
        """Déclenché quand un fichier/dossier est créé dans la source."""
        self.status_callback("Synchronisation...")
        if event.is_directory:
            self._create_directory(event.src_path)
        else:
            self._copy_file(event.src_path)
        self.status_callback("Surveillance active")

    def on_modified(self, event):
        """Déclenché quand un fichier est modifié dans la source."""
        if not event.is_directory:
            self.status_callback("Synchronisation...")
            self._copy_file(event.src_path)
            self.status_callback("Surveillance active")

    def on_deleted(self, event):
        """
        Déclenché quand un fichier/dossier est supprimé de la source.
        Au lieu de supprimer, on déplace vers l'archive.
        """
        self.status_callback("Archivage...")
        if event.is_directory:
            self._remove_directory(event.src_path)
        else:
            self._move_to_archive(event.src_path)
        self.status_callback("Surveillance active")

    def on_moved(self, event):
        """Déclenché quand un fichier/dossier est renommé ou déplacé."""
        self.status_callback("Synchronisation...")
        try:
            old_dest = self._get_dest_path(event.src_path)
            new_dest = self._get_dest_path(event.dest_path)

            if old_dest.exists():
                new_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_dest), str(new_dest))
                log_activity("rename", src=event.src_path, dest=event.dest_path)
                self._log_success("Déplacé/Renommé", event.dest_path)
        except Exception as e:
            self._log_error("DÉPLACEMENT", event.src_path, e)
        self.status_callback("Surveillance active")


# =============================================================================
# SYNCHRONISATION INITIALE
# =============================================================================

def initial_sync(source_path, dest_path, handler, progress_callback, log_callback,
                 archive_orphans=False):
    """
    Effectue une copie initiale complète de la source vers la destination.
    Appelée au démarrage de la surveillance.

    archive_orphans : si True, archive les fichiers présents dans la destination
    mais absents de la source (cas du retour de pause USB : pendant que la clé
    était débranchée, l'utilisateur a pu supprimer des fichiers de la source —
    ces fichiers existent encore sur la clé, on les déplace dans _Archive/
    pour rester cohérent avec le comportement habituel de l'app).
    Le dossier _Archive/ lui-même est exclu du balayage.
    """
    source = Path(source_path)
    dest = Path(dest_path)

    log_callback("📁 Analyse du dossier source...", "info")

    # Compte le nombre de fichiers pour la barre de progression
    total_files = sum(1 for _ in source.rglob("*") if _.is_file())
    copied_files = 0
    error_count = 0

    log_callback(f"📊 {total_files} fichiers à synchroniser", "info")

    # Parcours récursif avec os.walk (comme demandé dans les specs)
    for root, dirs, files in os.walk(source_path):
        # Crée la structure des dossiers
        for dir_name in dirs:
            src_dir = Path(root) / dir_name
            relative_path = src_dir.relative_to(source)
            dest_dir = dest / relative_path

            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                handler._log_error("CRÉATION DOSSIER", str(src_dir), e)
                error_count += 1

        # Copie les fichiers
        for file_name in files:
            src_file = Path(root) / file_name
            relative_path = src_file.relative_to(source)
            dest_file = dest / relative_path

            try:
                # Vérifie si le fichier doit être copié (nouveau ou modifié)
                should_copy = True
                if dest_file.exists():
                    # Compare les dates de modification
                    src_mtime = src_file.stat().st_mtime
                    dest_mtime = dest_file.stat().st_mtime
                    if dest_mtime >= src_mtime:
                        should_copy = False

                if should_copy:
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dest_file)
                    # Audit : on distingue copy_initial (sync de démarrage)
                    # de copy (event watchdog) pour pouvoir filtrer plus tard
                    try:
                        size = dest_file.stat().st_size
                    except OSError:
                        size = None
                    log_activity("copy_initial", src=src_file, dest=dest_file, size_bytes=size)
                    log_callback(f"✓ Copié: {file_name}", "success")

                copied_files += 1
                progress_callback(copied_files, total_files)

            except Exception as e:
                # GESTION D'ERREUR : on log et on continue avec le fichier suivant
                handler._log_error("COPIE INITIALE", str(src_file), e)
                error_count += 1
                copied_files += 1
                progress_callback(copied_files, total_files)

    # Balayage d'orphelins (uniquement au retour d'une pause USB) :
    # archive les fichiers qui sont en dest mais plus en source.
    if archive_orphans:
        log_callback("🔍 Recherche de fichiers orphelins (supprimés pendant la pause)...", "info")
        orphans_archived = 0
        for root, dirs, files in os.walk(dest):
            root_path = Path(root)
            # Exclure le dossier d'archive lui-même : ses sous-dossiers ne
            # doivent jamais être ré-archivés (boucle infinie).
            try:
                rel_parts = root_path.relative_to(dest).parts
            except ValueError:
                continue
            if ARCHIVE_FOLDER_NAME in rel_parts:
                # On ne descend pas dans _Archive/
                dirs[:] = []
                continue
            # On évite aussi de descendre dans _Archive/ depuis la racine
            if ARCHIVE_FOLDER_NAME in dirs:
                dirs.remove(ARCHIVE_FOLDER_NAME)

            for file_name in files:
                dest_file = root_path / file_name
                try:
                    rel_path = dest_file.relative_to(dest)
                    src_equiv = source / rel_path
                    if not src_equiv.exists():
                        # Fichier orphelin : archive (pas suppression)
                        try:
                            size = dest_file.stat().st_size
                        except OSError:
                            size = None
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        archive_name = f"{rel_path.stem}_{timestamp}{rel_path.suffix}"
                        archive_target = dest / ARCHIVE_FOLDER_NAME / rel_path.parent / archive_name
                        archive_target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(dest_file), str(archive_target))
                        log_activity("archive_orphan", src=dest_file, dest=archive_target, size_bytes=size)
                        orphans_archived += 1
                        log_callback(f"📦 Orphelin archivé: {file_name}", "info")
                except Exception as e:
                    handler._log_error("ARCHIVAGE ORPHELIN", str(dest_file), e)
                    error_count += 1
        if orphans_archived:
            log_callback(f"📦 {orphans_archived} orphelin(s) archivé(s)", "info")

    return total_files, error_count


# =============================================================================
# GÉNÉRATION DE DIAGNOSTIC (pour le bouton "?" dans l'UI)
# =============================================================================
#
# Le client n'a pas Internet : un fichier de diagnostic auto-suffisant doit
# pouvoir être copié sur clé USB et envoyé au support depuis un autre poste.
# Les fonctions ci-dessous sont écrites pour être TESTABLES indépendamment
# de l'UI (pas de tkinter), et tolérantes à toute erreur de lecture (un log
# manquant ne doit pas faire échouer la génération du diagnostic).

def _format_uptime(seconds):
    """Convertit une durée en secondes en chaîne lisible (ex: '3 j 4 h 22 min')."""
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} j")
    if hours or days:
        parts.append(f"{hours} h")
    parts.append(f"{minutes} min")
    return " ".join(parts)


def _read_last_n_lines(path, n):
    """
    Lit les N dernières lignes d'un fichier texte.
    Retourne une liste vide si le fichier est inaccessible (ne lève jamais).
    Implémentation simple (charge tout en mémoire) : suffisant pour nos logs
    qui font au plus quelques Mo. Pas optimal pour des Go mais ce n'est pas le cas.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return lines[-n:] if len(lines) > n else lines
    except Exception:
        return []


def _find_last_activity_event(log_dir, op_filter):
    """
    Cherche dans activity.log la dernière entrée dont l'op est dans op_filter.
    Retourne (timestamp_str, src_path) ou (None, None) si rien trouvé.
    """
    activity_file = log_dir / ACTIVITY_DIR_NAME / ACTIVITY_LOG_FILE_NAME
    for line in reversed(_read_last_n_lines(activity_file, 500)):
        try:
            entry = json.loads(line)
            if entry.get("op") in op_filter:
                return entry.get("ts"), entry.get("src", "—")
        except Exception:
            continue
    return None, None


def _state_to_french(state):
    """Mapping état machine → libellé court pour l'utilisateur."""
    return {
        STATE_RUNNING: "EN COURS",
        STATE_PAUSED_USB: "EN PAUSE — destination inaccessible",
        STATE_STOPPED: "ARRÊTÉE",
    }.get(state, state)


def build_diagnostic_text(state, source_path, dest_path, started_at, log_dir,
                          config_data=None):
    """
    Construit le contenu textuel du fichier de diagnostic.
    Pure fonction : prend un snapshot de l'état, retourne une string.
    Testable sans UI ni tkinter.
    """
    now = datetime.now()
    uptime_seconds = time.time() - started_at if started_at else 0
    last_copy_ts, last_copy_src = _find_last_activity_event(
        log_dir, {"copy", "copy_initial"}
    )

    last_error_lines = _read_last_n_lines(
        log_dir / ERRORS_DIR_NAME / ERRORS_LOG_FILE_NAME, 1
    )
    last_error = last_error_lines[-1].strip() if last_error_lines else "(aucune)"

    sections = []

    sections.append("=" * 60)
    sections.append("SimpleClone — fichier de diagnostic")
    sections.append("=" * 60)
    sections.append(f"Généré le      : {now.isoformat(timespec='seconds')}")
    sections.append(f"Version app    : {APP_VERSION}")
    sections.append(f"Python         : {sys.version.split()[0]}")
    sections.append(f"Plateforme     : {sys.platform}")
    sections.append("")

    sections.append("--- État de l'application ---")
    sections.append(f"Surveillance   : {_state_to_french(state)}")
    sections.append(f"Source         : {source_path or '(non sélectionnée)'}")
    sections.append(f"Destination    : {dest_path or '(non sélectionnée)'}")
    sections.append(f"Uptime         : {_format_uptime(uptime_seconds)}")
    sections.append(f"Démarrée le    : "
                    f"{datetime.fromtimestamp(started_at).isoformat(timespec='seconds') if started_at else '?'}")
    sections.append(f"Dernière copie : "
                    f"{last_copy_ts or 'aucune'} — {last_copy_src or ''}")
    sections.append(f"Dernière erreur: {last_error}")
    sections.append(f"Dossier logs   : {log_dir}")
    sections.append("")

    sections.append("--- Configuration ---")
    if config_data:
        for key, value in sorted(config_data.items()):
            sections.append(f"{key} = {value}")
    else:
        sections.append("(non disponible)")
    sections.append("")

    activity_tail = _read_last_n_lines(
        log_dir / ACTIVITY_DIR_NAME / ACTIVITY_LOG_FILE_NAME, 200
    )
    sections.append(f"--- Journal d'activité (200 dernières lignes) ---")
    if activity_tail:
        sections.extend(line.rstrip("\n") for line in activity_tail)
    else:
        sections.append("(vide ou inaccessible)")
    sections.append("")

    errors_tail = _read_last_n_lines(
        log_dir / ERRORS_DIR_NAME / ERRORS_LOG_FILE_NAME, 100
    )
    sections.append(f"--- Journal d'erreurs (100 dernières lignes) ---")
    if errors_tail:
        sections.extend(line.rstrip("\n") for line in errors_tail)
    else:
        sections.append("(vide ou inaccessible)")
    sections.append("")

    return "\n".join(sections)


def write_diagnostic_file(content, log_dir):
    """
    Écrit le diagnostic dans log/diagnostic/ avec un nom horodaté.
    Retourne le path écrit (ou lève l'exception si l'écriture est impossible —
    l'UI doit catcher pour afficher un message à l'utilisateur).
    """
    diag_dir = log_dir / DIAGNOSTIC_DIR_NAME
    diag_dir.mkdir(parents=True, exist_ok=True)
    filename = f"simpleclone-diagnostic-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    path = diag_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


def reveal_in_file_manager(path):
    """
    Ouvre le navigateur de fichiers du système et y met le fichier en évidence.
    Sur Windows : Explorer avec le fichier pré-sélectionné (highlighté).
    Sur macOS / Linux : on ouvre le dossier parent à défaut.
    Silencieux en cas d'échec : c'est un bonus UX, pas un chemin critique.
    """
    import subprocess
    try:
        if sys.platform == "win32":
            # /select, demande à Explorer de mettre le fichier en surbrillance
            subprocess.Popen(["explorer", f"/select,{path}"])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
        return True
    except Exception:
        return False
