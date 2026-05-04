# -*- coding: utf-8 -*-
"""
SimpleClone - Couche UI (Tkinter) + system tray (pystray).

Ce module dépend de tkinter et, optionnellement, de pystray + Pillow pour
l'icône dans la zone de notification. Il importe l'ensemble de la logique
métier depuis simpleclone_core.

Pourquoi `from simpleclone_core import *` plutôt que des imports explicites :
- la classe SimpleCloneApp utilise une trentaine de symboles du core
  (constantes d'état, ConfigManager, SyncHandler, log_activity, helpers de
  diagnostic, etc.), un import explicite ferait une liste interminable
  qu'il faudrait maintenir à chaque ajout
- core.py définit `__all__` pour contrôler ce qui sort
- aucun risque de collision : ce module ne définit que SimpleCloneApp et
  un helper d'icône, tous les autres noms viennent du core
"""

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
from tkinter import ttk

from watchdog.observers.polling import PollingObserver  # Plus fiable sur Windows/USB

from simpleclone_core import *  # noqa: F401,F403  — voir docstring du module

# `from X import *` filtre les noms underscored par défaut. On importe donc
# explicitement les helpers privés que le dialog d'aide et les méthodes UI
# consomment directement. Sans cette ligne, _show_help_dialog crashe avec un
# NameError silencieusement attrapé par tk → fenêtre Toplevel vide à l'écran
# (régression introduite et publiée en v0.11.0).
from simpleclone_core import (
    _resolve_log_dir,
    _format_uptime,
    _find_last_activity_event,
    _state_to_french,
)

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


# Couleurs d'UI (indicateur d'état + icône tray).
# Volontairement gardées ici (pas dans core) car purement présentationnelles.
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

        # Marqueur de début pour le calcul d'uptime + trace lifecycle.
        # log_activity est sûr ici : le logger est configuré au chargement du module.
        self._app_started_at = time.time()
        log_activity("app_start")

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

        # Première heartbeat planifiée : preuve périodique que l'app tourne.
        # On laisse passer un cycle complet (15 min) avant le premier tick :
        # l'entrée app_start ci-dessus tient lieu de "preuve" pour les 15 premières min.
        self.root.after(HEARTBEAT_INTERVAL_S * 1000, self._heartbeat_tick)

    def _heartbeat_tick(self):
        """
        Émet un heartbeat dans le log d'activité et reprogramme le suivant.
        En cas de crash silencieux ou de kill du process, l'absence de heartbeat
        > 1h (par exemple) est le signal pour qu'un humain investigue.
        """
        uptime_s = int(time.time() - self._app_started_at)
        log_activity("heartbeat", state=self.state, uptime_s=uptime_s)
        self.root.after(HEARTBEAT_INTERVAL_S * 1000, self._heartbeat_tick)

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

        # === HEADER avec bouton d'aide à droite ===
        # Volontairement minimaliste : titre vide à gauche pour que le "?" soit
        # bien dans le coin haut droite, comme demandé. C'est l'unique entrée
        # vers le diagnostic — on ne veut pas pop des dialogues automatiquement.
        header_frame = ttk.Frame(main_frame)
        header_frame.pack(fill=tk.X, pady=(0, 5))
        ttk.Button(
            header_frame, text="?", width=3,
            command=self._show_help_dialog,
        ).pack(side=tk.RIGHT)

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
                    f"Consultez {ERRORS_LOG_FILE_NAME} pour les détails."
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
    # AIDE / DIAGNOSTIC (bouton "?" en haut à droite)
    # -------------------------------------------------------------------------

    def _show_help_dialog(self):
        """
        Ouvre une fenêtre récapitulative + bouton de génération de diagnostic.
        Toutes les infos visibles ici sont volontairement répétées dans le
        fichier de diagnostic — pour permettre à l'utilisateur de prendre
        une photo de l'écran (s'il n'a pas accès à une clé USB sous la main).
        """
        dlg = tk.Toplevel(self.root)
        dlg.title("Aide SimpleClone")
        dlg.geometry("560x420")
        dlg.transient(self.root)
        dlg.resizable(False, False)

        container = ttk.Frame(dlg, padding=15)
        container.pack(fill=tk.BOTH, expand=True)

        # Snapshot de l'état actuel pour affichage
        log_dir = _resolve_log_dir()
        last_copy_ts, last_copy_src = _find_last_activity_event(
            log_dir, {"copy", "copy_initial"}
        )
        uptime_str = _format_uptime(time.time() - self._app_started_at)

        info_text = (
            f"État actuel\n"
            f"───────────\n"
            f"Surveillance   : {_state_to_french(self.state)}\n"
            f"Source         : {self.source_var.get() or '(non sélectionnée)'}\n"
            f"Destination    : {self.dest_var.get() or '(non sélectionnée)'}\n"
            f"Démarrée depuis: {uptime_str}\n"
            f"Dernière copie : {last_copy_ts or 'aucune'}"
            f"{' — ' + last_copy_src if last_copy_src and last_copy_src != '—' else ''}\n"
            f"Dossier logs   : {log_dir}\n"
        )

        info_label = tk.Label(
            container, text=info_text,
            justify=tk.LEFT, font=("Consolas", 9), anchor="w",
        )
        info_label.pack(fill=tk.X, anchor="w")

        explanation = ttk.Label(
            container,
            text=(
                "\nEn cas de problème\n"
                "──────────────────\n"
                "Cliquez sur le bouton ci-dessous pour créer un fichier de diagnostic.\n"
                "Le fichier sera enregistré dans le dossier des logs.\n"
                "Copiez-le sur une clé USB et envoyez-le à votre contact technique."
            ),
            justify=tk.LEFT, font=("Segoe UI", 9),
        )
        explanation.pack(fill=tk.X, anchor="w", pady=(5, 10))

        button_row = ttk.Frame(container)
        button_row.pack(fill=tk.X, pady=(10, 0))

        ttk.Button(
            button_row, text="Créer un fichier de diagnostic",
            command=lambda: self._create_diagnostic_file(dlg),
        ).pack(side=tk.LEFT)

        ttk.Button(
            button_row, text="Fermer",
            command=dlg.destroy,
        ).pack(side=tk.RIGHT)

        dlg.grab_set()  # modal

    def _create_diagnostic_file(self, parent_dialog):
        """
        Génère le fichier de diagnostic, l'écrit dans log/diagnostic/,
        et affiche une confirmation à l'utilisateur avec le chemin complet.
        """
        log_dir = _resolve_log_dir()
        try:
            content = build_diagnostic_text(
                state=self.state,
                source_path=self.source_var.get(),
                dest_path=self.dest_var.get(),
                started_at=self._app_started_at,
                log_dir=log_dir,
                config_data=self.config.data,
            )
            path = write_diagnostic_file(content, log_dir)
            self._log(f"📄 Diagnostic créé : {path.name}", "info")
            # Ouvre l'Explorateur (ou xdg-open) avec le fichier en évidence
            # AVANT le messagebox : la fenêtre Explorer apparaît sous le dialog,
            # l'utilisateur la trouvera juste en fermant le messagebox.
            reveal_in_file_manager(path)
            messagebox.showinfo(
                "Diagnostic créé",
                f"Le fichier de diagnostic a été créé :\n\n"
                f"{path}\n\n"
                f"L'Explorateur de fichiers s'ouvre sur ce fichier.\n"
                f"Copiez-le sur une clé USB et envoyez-le à votre contact technique.",
                parent=parent_dialog,
            )
        except Exception as e:
            messagebox.showerror(
                "Erreur",
                f"Impossible de créer le fichier de diagnostic :\n{e}\n\n"
                f"Demandez de l'aide à votre contact technique en lui décrivant "
                f"le problème observé.",
                parent=parent_dialog,
            )

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
        # Trace lifecycle : "fin propre" — distingue d'un crash silencieux dans l'audit.
        # On émet AVANT le destroy pour s'assurer que la ligne est bien écrite.
        log_activity("app_stop", uptime_s=int(time.time() - self._app_started_at))

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
