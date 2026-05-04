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
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from tkinter import ttk

# Watchdog : bibliothèque de surveillance de fichiers
# Installation : pip install watchdog
from watchdog.observers.polling import PollingObserver  # Plus fiable sur Windows/USB
from watchdog.events import FileSystemEventHandler

# =============================================================================
# CONFIGURATION
# =============================================================================

ARCHIVE_FOLDER_NAME = "_Archive"  # Dossier où sont déplacés les fichiers supprimés
LOG_FILE_NAME = "SimpleClone_Errors.log"
DEFAULT_POLLING_INTERVAL = 2  # Intervalle de vérification en secondes (polling)


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
# GESTIONNAIRE D'ÉVÉNEMENTS FICHIERS
# =============================================================================

class SyncHandler(FileSystemEventHandler):
    """
    Gère les événements du système de fichiers détectés par watchdog.
    Réplique chaque changement de la source vers la destination.
    """

    def __init__(self, source_path, dest_path, log_callback, status_callback):
        super().__init__()
        self.source_path = Path(source_path)
        self.dest_path = Path(dest_path)
        self.archive_path = self.dest_path / ARCHIVE_FOLDER_NAME
        self.log_callback = log_callback  # Fonction pour afficher les logs dans l'interface
        self.status_callback = status_callback  # Fonction pour mettre à jour le statut
        self.error_count = 0

        # Chemin du fichier de log des erreurs (à côté du script)
        self.error_log_path = Path(sys.executable).parent / LOG_FILE_NAME \
            if getattr(sys, 'frozen', False) else Path(__file__).parent / LOG_FILE_NAME

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
        """
        self.error_count += 1
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        error_msg = f"[{timestamp}] {operation} ERREUR: {path}\n    -> {str(error)}\n"

        # Écriture dans le fichier log
        try:
            with open(self.error_log_path, "a", encoding="utf-8") as f:
                f.write(error_msg)
        except Exception:
            pass  # Même l'écriture du log ne doit pas bloquer

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
                archive_file = self._get_archive_path(src_path)
                archive_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dest_file), str(archive_file))
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
                self._log_success("Déplacé/Renommé", event.dest_path)
        except Exception as e:
            self._log_error("DÉPLACEMENT", event.src_path, e)
        self.status_callback("Surveillance active")


# =============================================================================
# SYNCHRONISATION INITIALE
# =============================================================================

def initial_sync(source_path, dest_path, handler, progress_callback, log_callback):
    """
    Effectue une copie initiale complète de la source vers la destination.
    Appelée au démarrage de la surveillance.
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
                    log_callback(f"✓ Copié: {file_name}", "success")

                copied_files += 1
                progress_callback(copied_files, total_files)

            except Exception as e:
                # GESTION D'ERREUR : on log et on continue avec le fichier suivant
                handler._log_error("COPIE INITIALE", str(src_file), e)
                error_count += 1
                copied_files += 1
                progress_callback(copied_files, total_files)

    return total_files, error_count


# =============================================================================
# INTERFACE GRAPHIQUE
# =============================================================================

class SimpleCloneApp:
    """Application principale avec interface Tkinter."""

    def __init__(self, root):
        self.root = root
        self.root.title("SimpleClone")
        self.root.geometry("700x500")
        self.root.minsize(600, 400)

        # Config persistante (chargée avant la création des widgets pour pré-remplir)
        self.config = ConfigManager()
        self.config.load()

        # Variables — initialisées avec les valeurs de la config si disponibles
        self.source_var = tk.StringVar(value=self.config.get("source_path"))
        self.dest_var = tk.StringVar(value=self.config.get("dest_path"))
        self.status_var = tk.StringVar(value="Prêt - Sélectionnez les dossiers")
        self.observer = None
        self.is_running = False

        # Style
        self.root.configure(bg="#f0f0f0")

        self._create_widgets()

        # Gestion de la fermeture
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

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

        # === BOUTON PRINCIPAL ===
        self.start_button = tk.Button(
            main_frame,
            text="▶ DÉMARRER LA SURVEILLANCE",
            command=self._toggle_surveillance,
            font=("Segoe UI", 12, "bold"),
            bg="#4CAF50",
            fg="white",
            activebackground="#45a049",
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

    def _log(self, message, tag="info"):
        """Ajoute un message dans la zone de log."""
        self.log_text.configure(state=tk.NORMAL)
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n", tag)
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

    def _toggle_surveillance(self):
        """Démarre ou arrête la surveillance."""
        if self.is_running:
            self._stop_surveillance()
        else:
            self._start_surveillance()

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

        self.is_running = True
        self.start_button.configure(
            text="⏹ ARRÊTER LA SURVEILLANCE",
            bg="#f44336"
        )
        self.status_indicator.configure(fg="#4CAF50")
        self.warning_frame.pack(fill=tk.X, pady=(0, 5))  # Affiche l'avertissement USB
        self._update_status("Synchronisation initiale...")
        self._log("🚀 Démarrage de la surveillance...", "info")

        # Lance la synchronisation initiale dans un thread séparé
        # pour ne pas bloquer l'interface
        threading.Thread(target=self._run_sync, args=(source, dest), daemon=True).start()

    def _run_sync(self, source, dest):
        """Exécute la synchronisation (dans un thread séparé)."""
        # Crée le gestionnaire d'événements
        handler = SyncHandler(
            source,
            dest,
            lambda msg, tag: self.root.after(0, lambda: self._log(msg, tag)),
            self._update_status
        )

        # Synchronisation initiale
        self._log("📋 Copie initiale en cours...", "info")
        total, errors = initial_sync(
            source,
            dest,
            handler,
            self._update_progress,
            lambda msg, tag: self.root.after(0, lambda: self._log(msg, tag))
        )

        # Message de fin de synchro initiale
        if errors > 0:
            self._log(f"⚠️ Copie initiale terminée avec {errors} erreur(s)", "warning")
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

    def _stop_surveillance(self):
        """Arrête la surveillance."""
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=2)
            self.observer = None

        self.is_running = False
        self.start_button.configure(
            text="▶ DÉMARRER LA SURVEILLANCE",
            bg="#4CAF50"
        )
        self.status_indicator.configure(fg="gray")
        self.warning_frame.pack_forget()  # Cache l'avertissement USB
        self._update_status("Surveillance arrêtée")
        self._log("⏹️ Surveillance arrêtée", "warning")

    def _on_closing(self):
        """Gère la fermeture de l'application."""
        if self.is_running:
            if messagebox.askokcancel(
                "Quitter",
                "La surveillance est en cours.\nVoulez-vous vraiment quitter ?"
            ):
                self._stop_surveillance()
                self.root.destroy()
        else:
            self.root.destroy()


# =============================================================================
# POINT D'ENTRÉE
# =============================================================================

if __name__ == "__main__":
    root = tk.Tk()
    app = SimpleCloneApp(root)
    root.mainloop()
