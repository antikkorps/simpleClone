# -*- coding: utf-8 -*-
"""
SimpleClone - Outil de synchronisation unidirectionnelle de dossiers
Surveille un dossier source et réplique les changements vers une destination.
Conçu pour tourner en continu (H24).
"""

import os
import sys
import json
import shutil
import threading
import time
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from tkinter import ttk

# Watchdog : bibliothèque de surveillance de fichiers
# Installation : pip install watchdog
from watchdog.observers.polling import PollingObserver  # Plus fiable sur Windows/USB
from watchdog.events import FileSystemEventHandler

# System tray (pystray + Pillow) : optionnel.
# Si non installé, l'app fonctionne sans icône dans la zone de notification.
# On limite le tray à Windows : sur WSL/Linux pystray a besoin de X11 et plante
# souvent dans les environnements headless.
try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = (sys.platform == "win32")
except ImportError:
    TRAY_AVAILABLE = False

# =============================================================================
# CONFIGURATION
# =============================================================================

ARCHIVE_FOLDER_NAME = "_Archive"  # Dossier où sont déplacés les fichiers supprimés

# Structure des logs (à côté de l'exe ou dans %APPDATA% en fallback) :
#   log/
#   ├── errors/   → SimpleClone_Errors.log (rotation par taille)
#   └── activity/ → activity.log + activity.log.YYYY-MM-DD (rotation quotidienne, 6 ans)
LOG_DIR_NAME = "log"
ERRORS_DIR_NAME = "errors"
ACTIVITY_DIR_NAME = "activity"
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

# États de la surveillance
STATE_STOPPED = "stopped"
STATE_RUNNING = "running"
STATE_PAUSED_USB = "paused_usb"  # Destination inaccessible (clé débranchée)

# Couleurs des icônes (zone de notification + indicateur UI)
COLOR_STOPPED = "#808080"   # gris
COLOR_RUNNING = "#4CAF50"   # vert
COLOR_PAUSED = "#f57c00"    # orange


def _make_tray_icon_image(color_hex):
    """Génère une icône 64x64 (disque coloré) pour la zone de notification."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))  # Fond transparent
    draw = ImageDraw.Draw(img)
    draw.ellipse((6, 6, 58, 58), fill=color_hex, outline="#222222", width=2)
    return img


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
    notification (logique gérée par le system tray, étape 5).
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    script = Path(__file__).resolve()
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
# INTERFACE GRAPHIQUE
# =============================================================================

class SimpleCloneApp:
    """Application principale avec interface Tkinter."""

    def __init__(self, root, args=None):
        self.root = root
        self.root.title("SimpleClone")
        self.root.geometry("700x500")
        self.root.minsize(600, 400)

        # Args CLI (ex: --minimized lors d'un démarrage automatique Windows)
        self.args = args

        # Config persistante (chargée avant la création des widgets pour pré-remplir)
        self.config = ConfigManager()
        self.config.load()

        # Synchronise l'état "autostart Windows" de la config avec le registre :
        # le registre est la source de vérité (l'utilisateur peut l'avoir modifié
        # à la main, ou la dernière session a pu se terminer mal).
        if sys.platform == "win32":
            actual_autostart = is_windows_autostart_enabled()
            if actual_autostart != self.config.get("autostart_windows"):
                self.config.set("autostart_windows", actual_autostart)

        # Variables — initialisées avec les valeurs de la config si disponibles
        self.source_var = tk.StringVar(value=self.config.get("source_path"))
        self.dest_var = tk.StringVar(value=self.config.get("dest_path"))
        self.autostart_windows_var = tk.BooleanVar(value=self.config.get("autostart_windows"))
        self.autostart_surveillance_var = tk.BooleanVar(
            value=self.config.get("autostart_surveillance")
        )
        self.status_var = tk.StringVar(value="Prêt - Sélectionnez les dossiers")
        self.observer = None
        self.state = STATE_STOPPED
        self.handler = None  # Référence au SyncHandler courant (pour reset des flags)
        self._retry_thread = None  # Thread de retry quand la dest est inaccessible
        self.tray_icon = None  # Icône zone de notification (None si pystray indispo)
        self._tray_images = {}  # Cache des images d'icône par état

        # Style
        self.root.configure(bg="#f0f0f0")

        self._create_widgets()

        # Gestion de la fermeture
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

        # Affiche le chemin du dossier de logs au démarrage : pratique pour
        # l'admin si l'app est tombée sur le fallback %APPDATA%.
        try:
            self._log(f"📁 Logs : {_resolve_log_dir()}", "info")
        except Exception:
            pass

        # Initialisation de l'icône dans la zone de notification (Windows uniquement).
        # Si pystray n'est pas dispo, l'app continue sans tray (clic sur X = quitter).
        self._setup_tray()

        # --minimized : démarrage caché (typique de l'autostart Windows). On ne
        # cache la fenêtre que si on a un tray pour permettre de la rouvrir,
        # sinon l'utilisateur n'aurait aucun moyen de revenir à l'UI.
        if args and getattr(args, "minimized", False):
            if self.tray_icon is not None:
                self.root.withdraw()
            else:
                self._log(
                    "⚠ Démarrage --minimized ignoré : pystray non disponible",
                    "warning"
                )

        # Démarrage automatique de la surveillance si demandé ET chemins valides.
        # Délai 500ms pour laisser l'UI s'afficher avant de lancer le thread de sync.
        if self._should_autostart_surveillance():
            self.root.after(500, self._start_surveillance)

    def _should_autostart_surveillance(self):
        """Détermine si on doit lancer la surveillance automatiquement au boot."""
        if not self.config.get("autostart_surveillance"):
            return False
        source = self.source_var.get()
        dest = self.dest_var.get()
        if not source or not dest:
            return False
        if not os.path.isdir(source):
            return False
        return True

    def _create_widgets(self):
        """Crée tous les widgets de l'interface."""

        # Frame principal avec padding
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # === SECTION SOURCE ===
        source_frame = ttk.LabelFrame(main_frame, text="Dossier Source (à surveiller)", padding="5")
        source_frame.pack(fill=tk.X, pady=(0, 10))

        self.source_entry = ttk.Entry(source_frame, textvariable=self.source_var, state="readonly")
        self.source_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        ttk.Button(source_frame, text="Parcourir...", command=self._browse_source).pack(side=tk.RIGHT)

        # === SECTION DESTINATION ===
        dest_frame = ttk.LabelFrame(main_frame, text="Dossier Destination (ex: clé USB)", padding="5")
        dest_frame.pack(fill=tk.X, pady=(0, 10))

        self.dest_entry = ttk.Entry(dest_frame, textvariable=self.dest_var, state="readonly")
        self.dest_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        ttk.Button(dest_frame, text="Parcourir...", command=self._browse_dest).pack(side=tk.RIGHT)

        # === OPTIONS DE DÉMARRAGE ===
        options_frame = ttk.Frame(main_frame)
        options_frame.pack(fill=tk.X, pady=(0, 10))

        self.cb_autostart_windows = ttk.Checkbutton(
            options_frame,
            text="Démarrer avec Windows",
            variable=self.autostart_windows_var,
            command=self._on_autostart_windows_toggle,
        )
        self.cb_autostart_windows.pack(side=tk.LEFT, padx=(0, 20))

        # Hors Windows : on grise la checkbox (utile pour dev en WSL)
        if sys.platform != "win32":
            self.cb_autostart_windows.configure(state="disabled")

        self.cb_autostart_surveillance = ttk.Checkbutton(
            options_frame,
            text="Lancer la surveillance automatiquement",
            variable=self.autostart_surveillance_var,
            command=self._on_autostart_surveillance_toggle,
        )
        self.cb_autostart_surveillance.pack(side=tk.LEFT)

        # === BOUTON PRINCIPAL ===
        # bg piloté par _set_state (vert au repos, orange quand actif/en pause)
        self.start_button = tk.Button(
            main_frame,
            text="▶ DÉMARRER LA SURVEILLANCE",
            command=self._toggle_surveillance,
            font=("Segoe UI", 12, "bold"),
            bg=COLOR_RUNNING,
            fg="white",
            activeforeground="white",
            height=2,
            cursor="hand2"
        )
        self.start_button.pack(fill=tk.X, pady=10)

        # === BARRE DE PROGRESSION ===
        self.progress = ttk.Progressbar(main_frame, mode="determinate")
        self.progress.pack(fill=tk.X, pady=(0, 5))

        # === STATUT ===
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=(0, 10))

        self.status_indicator = tk.Label(status_frame, text="●", fg="gray", font=("Segoe UI", 14))
        self.status_indicator.pack(side=tk.LEFT)

        self.status_label = ttk.Label(status_frame, textvariable=self.status_var, font=("Segoe UI", 10))
        self.status_label.pack(side=tk.LEFT, padx=5)

        # === AVERTISSEMENT USB ===
        self.warning_frame = tk.Frame(main_frame, bg="#fff3cd", padx=10, pady=5)
        self.warning_label = tk.Label(
            self.warning_frame,
            text="⚠️ IMPORTANT : Arrêtez la surveillance AVANT de débrancher la clé USB !",
            bg="#fff3cd",
            fg="#856404",
            font=("Segoe UI", 9, "bold")
        )
        self.warning_label.pack()
        # Caché par défaut, affiché quand la surveillance est active

        # === ZONE DE LOG ===
        log_frame = ttk.LabelFrame(main_frame, text="Journal d'activité", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            height=10,
            font=("Consolas", 9),
            state=tk.DISABLED,
            wrap=tk.WORD
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # Tags pour les couleurs dans le log
        self.log_text.tag_configure("success", foreground="#2e7d32")
        self.log_text.tag_configure("error", foreground="#c62828")
        self.log_text.tag_configure("info", foreground="#1565c0")
        self.log_text.tag_configure("warning", foreground="#f57c00")

    def _browse_source(self):
        """Ouvre le dialogue pour sélectionner le dossier source."""
        folder = filedialog.askdirectory(title="Sélectionner le dossier source")
        if folder:
            self.source_var.set(folder)
            self.config.set("source_path", folder)
            self._log(f"📂 Source sélectionnée: {folder}", "info")

    def _browse_dest(self):
        """Ouvre le dialogue pour sélectionner le dossier destination."""
        folder = filedialog.askdirectory(title="Sélectionner le dossier destination")
        if folder:
            self.dest_var.set(folder)
            self.config.set("dest_path", folder)
            self._log(f"📂 Destination sélectionnée: {folder}", "info")

    def _on_autostart_windows_toggle(self):
        """Active/désactive le démarrage avec Windows (registre HKCU)."""
        enabled = self.autostart_windows_var.get()
        success = set_windows_autostart(enabled)
        if not success:
            # Échec : on remet la checkbox dans son ancien état et on prévient
            self.autostart_windows_var.set(not enabled)
            messagebox.showerror(
                "Erreur",
                "Impossible de modifier le démarrage automatique.\n"
                "Vérifiez que vous êtes bien sur Windows."
            )
            return
        self.config.set("autostart_windows", enabled)
        self._log(
            f"🪟 Démarrage avec Windows : {'activé' if enabled else 'désactivé'}",
            "info"
        )

    def _on_autostart_surveillance_toggle(self):
        """Mémorise le choix : démarrer la surveillance dès l'ouverture de l'app."""
        enabled = self.autostart_surveillance_var.get()
        self.config.set("autostart_surveillance", enabled)
        self._log(
            f"⚙ Surveillance auto au démarrage : {'activée' if enabled else 'désactivée'}",
            "info"
        )

    def _log(self, message, tag="info"):
        """
        Ajoute un message dans la zone de log de l'UI.
        Plafonne le nombre de lignes pour éviter une fuite mémoire en H24 :
        au-delà de MAX_LOG_LINES_UI, on supprime un lot du début (par lots
        plutôt que ligne par ligne, c'est nettement moins coûteux).
        """
        self.log_text.configure(state=tk.NORMAL)
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n", tag)

        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES_UI:
            # On supprime LOG_TRIM_BATCH lignes (en plus du surplus) pour
            # ne pas retomber dans la condition au prochain insert.
            excess = line_count - MAX_LOG_LINES_UI + LOG_TRIM_BATCH
            self.log_text.delete("1.0", f"{excess + 1}.0")

        self.log_text.see(tk.END)  # Auto-scroll vers le bas
        self.log_text.configure(state=tk.DISABLED)

    def _update_status(self, status):
        """Met à jour le texte de statut (thread-safe)."""
        self.root.after(0, lambda: self.status_var.set(status))

    def _update_progress(self, current, total):
        """Met à jour la barre de progression (thread-safe)."""
        if total > 0:
            progress = (current / total) * 100
            self.root.after(0, lambda: self.progress.configure(value=progress))

    def _set_state(self, new_state):
        """
        Centralise les transitions d'état et l'UI associée.
        Doit être appelée depuis le main thread (utiliser root.after sinon).
        Le bouton "Arrêter" est orange (et non rouge) : le rouge dramatisait
        une action triviale (la surveillance se relance en un clic) et créait
        un faux signal d'alerte alors que le statut indicateur est vert.
        """
        self.state = new_state
        if new_state == STATE_RUNNING:
            self.start_button.configure(text="⏹ ARRÊTER LA SURVEILLANCE", bg=COLOR_PAUSED)
            self.status_indicator.configure(fg=COLOR_RUNNING)
            self.warning_frame.pack(fill=tk.X, pady=(0, 5))
        elif new_state == STATE_PAUSED_USB:
            self.start_button.configure(text="⏹ ARRÊTER LA SURVEILLANCE", bg=COLOR_PAUSED)
            self.status_indicator.configure(fg=COLOR_PAUSED)
            self.warning_frame.pack(fill=tk.X, pady=(0, 5))
            self.status_var.set("⏸ En pause — destination inaccessible")
        else:  # STATE_STOPPED
            self.start_button.configure(text="▶ DÉMARRER LA SURVEILLANCE", bg=COLOR_RUNNING)
            self.status_indicator.configure(fg="gray")
            self.warning_frame.pack_forget()

        # Met à jour l'icône de la zone de notification + le menu (label dynamique)
        if self.tray_icon is not None:
            self.tray_icon.icon = self._tray_images.get(new_state)
            try:
                self.tray_icon.update_menu()
            except Exception:
                pass  # update_menu peut être absent selon la version de pystray

    def _toggle_surveillance(self):
        """Démarre ou arrête la surveillance (selon l'état courant)."""
        if self.state == STATE_STOPPED:
            self._start_surveillance()
        else:
            # RUNNING ou PAUSED_USB → arrêt manuel
            self._stop_surveillance()

    def _start_surveillance(self):
        """Démarre la surveillance du dossier source."""
        source = self.source_var.get()
        dest = self.dest_var.get()

        # Validation des chemins
        if not source or not dest:
            messagebox.showwarning("Attention", "Veuillez sélectionner les dossiers source et destination.")
            return

        if not os.path.isdir(source):
            messagebox.showerror("Erreur", "Le dossier source n'existe pas.")
            return

        if source == dest:
            messagebox.showerror("Erreur", "Les dossiers source et destination doivent être différents.")
            return

        # Crée le dossier destination si nécessaire
        try:
            os.makedirs(dest, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Erreur", f"Impossible de créer le dossier destination:\n{e}")
            return

        self._set_state(STATE_RUNNING)
        self._update_status("Synchronisation initiale...")
        self._log("🚀 Démarrage de la surveillance...", "info")

        # Lance la synchronisation initiale dans un thread séparé
        # pour ne pas bloquer l'interface
        threading.Thread(
            target=self._run_sync,
            args=(source, dest),
            kwargs={"archive_orphans": False},
            daemon=True,
        ).start()

    def _run_sync(self, source, dest, archive_orphans=False):
        """Exécute la synchronisation initiale puis lance l'observer (thread séparé)."""
        # Crée le gestionnaire d'événements (avec callback pour débranchement USB)
        handler = SyncHandler(
            source,
            dest,
            lambda msg, tag: self.root.after(0, lambda: self._log(msg, tag)),
            self._update_status,
            on_dest_lost=lambda: self.root.after(0, self._on_dest_lost),
        )
        self.handler = handler

        # Synchronisation initiale
        self._log("📋 Copie initiale en cours...", "info")
        total, errors = initial_sync(
            source,
            dest,
            handler,
            self._update_progress,
            lambda msg, tag: self.root.after(0, lambda: self._log(msg, tag)),
            archive_orphans=archive_orphans,
        )

        # Si on a perdu la dest pendant la sync initiale, on n'enchaîne pas l'observer
        if self.state != STATE_RUNNING:
            return

        # Message de fin de synchro initiale
        if errors > 0:
            self._log(f"⚠️ Copie initiale terminée avec {errors} erreur(s)", "warning")
            # Pas de popup au retour de pause USB (l'utilisateur n'est peut-être pas devant l'écran)
            if not archive_orphans:
                self.root.after(0, lambda: messagebox.showwarning(
                    "Copie initiale",
                    f"Copie initiale terminée, mais {errors} fichier(s) n'ont pas pu être copiés.\n"
                    f"Consultez {LOG_FILE_NAME} pour les détails."
                ))
        else:
            self._log(f"✅ Copie initiale terminée ({total} fichiers)", "success")

        # Démarre la surveillance continue avec watchdog
        # PollingObserver vérifie les changements toutes les X secondes (plus fiable sur Windows/USB)
        polling_interval = self.config.get("polling_interval") or DEFAULT_POLLING_INTERVAL
        self.observer = PollingObserver(timeout=polling_interval)
        self.observer.schedule(handler, source, recursive=True)
        self.observer.start()

        self.root.after(0, lambda: self._log(
            f"🔄 Mode polling actif (vérification toutes les {polling_interval}s)", "info"
        ))

        self._update_status("Surveillance active")
        self._log("👁️ Surveillance active - En attente de changements...", "info")
        self.root.after(0, lambda: self.progress.configure(value=0))

    def _on_dest_lost(self):
        """
        Appelé (depuis le main thread) quand le SyncHandler détecte que la
        destination est devenue inaccessible. Bascule en PAUSED_USB et lance
        un thread de retry qui vérifiera périodiquement le retour de la dest.
        """
        if self.state != STATE_RUNNING:
            return  # Déjà en pause ou arrêté : rien à faire

        # Stop l'observer (peut lever des exceptions sur la dest disparue → on ignore)
        if self.observer:
            try:
                self.observer.stop()
                self.observer.join(timeout=2)
            except Exception:
                pass
            self.observer = None

        self._set_state(STATE_PAUSED_USB)
        self._log("⏸ Surveillance en pause — destination inaccessible", "warning")

        # Lance le thread de retry (un seul à la fois)
        if self._retry_thread is None or not self._retry_thread.is_alive():
            self._retry_thread = threading.Thread(target=self._dest_retry_loop, daemon=True)
            self._retry_thread.start()

    def _dest_retry_loop(self):
        """
        Boucle exécutée dans un thread daemon : vérifie périodiquement si la
        destination est revenue. Sort dès que l'état n'est plus PAUSED_USB
        (l'utilisateur a cliqué sur Arrêter, ou la reprise a été déclenchée).
        """
        dest = self.dest_var.get()
        while self.state == STATE_PAUSED_USB:
            time.sleep(DEST_RETRY_INTERVAL)
            if self.state != STATE_PAUSED_USB:
                return
            try:
                if Path(dest).is_dir():
                    # Dest revenue : on déclenche la reprise depuis le main thread
                    self.root.after(0, self._resume_from_usb_pause)
                    return
            except OSError:
                pass  # Dest toujours absente, on continue d'attendre

    def _resume_from_usb_pause(self):
        """
        Reprise depuis PAUSED_USB → RUNNING.
        Relance une sync complète AVEC archivage des orphelins (au cas où des
        fichiers ont été supprimés de la source pendant la pause).
        """
        if self.state != STATE_PAUSED_USB:
            return  # État changé entre temps (arrêt manuel) : on annule

        source = self.source_var.get()
        dest = self.dest_var.get()
        if not source or not os.path.isdir(source):
            return

        self._set_state(STATE_RUNNING)
        self._log("✅ Destination détectée — reprise de la surveillance", "success")
        self._update_status("Resynchronisation après reconnexion...")

        threading.Thread(
            target=self._run_sync,
            args=(source, dest),
            kwargs={"archive_orphans": True},
            daemon=True,
        ).start()

    def _stop_surveillance(self):
        """Arrête la surveillance (depuis n'importe quel état actif)."""
        previous_state = self.state
        # Le passage à STATE_STOPPED fait sortir le thread retry de sa boucle
        self._set_state(STATE_STOPPED)

        if self.observer:
            try:
                self.observer.stop()
                self.observer.join(timeout=2)
            except Exception:
                pass
            self.observer = None

        self.handler = None
        self._update_status("Surveillance arrêtée")
        if previous_state == STATE_PAUSED_USB:
            self._log("⏹️ Surveillance arrêtée (était en pause USB)", "warning")
        else:
            self._log("⏹️ Surveillance arrêtée", "warning")

    # -------------------------------------------------------------------------
    # ZONE DE NOTIFICATION (system tray)
    # -------------------------------------------------------------------------

    def _setup_tray(self):
        """Initialise l'icône dans la zone de notification (Windows uniquement)."""
        if not TRAY_AVAILABLE:
            return

        self._tray_images = {
            STATE_STOPPED: _make_tray_icon_image(COLOR_STOPPED),
            STATE_RUNNING: _make_tray_icon_image(COLOR_RUNNING),
            STATE_PAUSED_USB: _make_tray_icon_image(COLOR_PAUSED),
        }

        # Le label "Démarrer/Arrêter" se calcule à la volée, ce qui permet à
        # update_menu() de refléter l'état courant à chaque changement.
        def toggle_label(_item):
            return ("Arrêter la surveillance"
                    if self.state != STATE_STOPPED
                    else "Démarrer la surveillance")

        menu = pystray.Menu(
            pystray.MenuItem("Afficher SimpleClone", self._tray_show, default=True),
            pystray.MenuItem(toggle_label, self._tray_toggle_surveillance),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quitter", self._tray_quit),
        )

        self.tray_icon = pystray.Icon(
            "SimpleClone",
            self._tray_images[STATE_STOPPED],
            "SimpleClone",
            menu,
        )

        # pystray.Icon.run() est bloquant : on le lance dans un thread daemon
        threading.Thread(target=self._run_tray_safely, daemon=True).start()

    def _run_tray_safely(self):
        """Wrapper pour ne pas crasher l'app si pystray échoue à s'initialiser."""
        try:
            self.tray_icon.run()
        except Exception:
            self.tray_icon = None  # Désactive le tray, l'UI reste fonctionnelle

    def _tray_show(self, icon=None, item=None):
        """Restaure la fenêtre principale depuis la zone de notification."""
        self.root.after(0, lambda: (self.root.deiconify(), self.root.lift()))

    def _tray_toggle_surveillance(self, icon=None, item=None):
        """Démarre/arrête la surveillance depuis le menu tray."""
        self.root.after(0, self._toggle_surveillance)

    def _tray_quit(self, icon=None, item=None):
        """Quitter définitivement (depuis le menu tray)."""
        self.root.after(0, self._quit_app)

    def _quit_app(self):
        """Sortie propre de l'application : stop surveillance + tray + UI."""
        if self.state != STATE_STOPPED:
            self._stop_surveillance()
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self.root.destroy()

    def _on_closing(self):
        """
        Clic sur la croix (X) de la fenêtre.
        Si le tray est actif → on cache la fenêtre, l'app continue en arrière-plan.
        Sinon → comportement classique (confirmation puis fermeture).
        """
        if self.tray_icon is not None:
            self.root.withdraw()
            return

        if self.state != STATE_STOPPED:
            if messagebox.askokcancel(
                "Quitter",
                "La surveillance est en cours.\nVoulez-vous vraiment quitter ?"
            ):
                self._quit_app()
        else:
            self._quit_app()


# =============================================================================
# POINT D'ENTRÉE
# =============================================================================

def _parse_args():
    """Parse les arguments CLI. --minimized est passé par l'autostart Windows."""
    import argparse
    parser = argparse.ArgumentParser(
        description="SimpleClone - Synchronisation unidirectionnelle de dossiers"
    )
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="Démarre l'application dans la zone de notification (pas de fenêtre)."
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    root = tk.Tk()
    app = SimpleCloneApp(root, args=args)
    root.mainloop()
